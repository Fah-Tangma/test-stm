import io
import re
import pandas as pd
import pikepdf
import pdfplumber
import streamlit as st
from pikepdf import PasswordError

st.set_page_config(page_title="PDF Statement Converter", layout="wide")

# ================= 1. ฟังก์ชันช่วยเหลือทั่วไป =================
def str_to_float(val_str):
    if val_str in [None, "", "-", " "]: return 0.0
    try:
        clean_val = str(val_str).replace(',', '').strip()
        return float(clean_val)
    except:
        return 0.0

# ================= 2. Logic สำหรับกสิกรไทย (KBank) =================
# รูปแบบ: วันที่, เวลา, รายการ, ถอนเงิน, ฝากเงิน, ยอดคงเหลือ, รายละเอียด
def parse_kbank_pdf(pdf_stream):
    all_parsed_rows = []
    table_headers = ["เวลา/", "วันที่มีผล", "รายการ", "ถอนเงิน", "ฝากเงิน", "ยอดคงเหลือ"]

    with pdfplumber.open(pdf_stream) as pdf_obj:
        for page in pdf_obj.pages:
            text = page.extract_text()
            if not text: continue
            lines = text.split('\n')
            is_in_table = False 

            for line in lines:
                line = line.strip()
                if not line: continue
                if any(kw in line for kw in table_headers):
                    is_in_table = True
                    continue
                
                date_match = re.match(r'^(\d{2}-\d{2}-\d{2})', line)
                if date_match and is_in_table:
                    date = date_match.group(1)
                    time_match = re.search(r'(\d{2}:\d{2})', line)
                    time = time_match.group(1) if time_match else ""
                    amounts = re.findall(r'[\d,]+\.\d{2}', line)
                    
                    temp_text = line.replace(date, "").strip()
                    if time: temp_text = temp_text.replace(time, "", 1).strip()
                    desc = temp_text.split(amounts[0])[0].strip() if amounts else temp_text
                    
                    withdraw, deposit, balance = 0.0, 0.0, 0.0
                    
                    if len(amounts) == 1:
                        balance = str_to_float(amounts[0])
                    elif len(amounts) >= 2:
                        val = str_to_float(amounts[0])
                        is_dep = any(kw in desc for kw in ["รับเงิน", "คืนเงิน", "ฝาก", "เงินคืน", "Thai QR", "รับโอนเงิน"])
                        if is_dep: deposit = val
                        else: withdraw = val
                        balance = str_to_float(amounts[-1])

                    detail = line.split(amounts[-1])[-1].strip() if amounts else ""
                    all_parsed_rows.append([date, time, desc, withdraw, deposit, balance, detail])

    return all_parsed_rows

# ================= 3. Logic สำหรับไทยพาณิชย์ (SCB) =================
# รูปแบบ: วันที่, เวลา, Code, ยอดเงินหักบัญชี, ยอดเงินเข้าบัญชี, ยอดคงเหลือ, ช่องทาง, รายละเอียด
def parse_scb_pdf(pdf_stream):
    all_parsed_rows = []
    bf_keywords = ["ยอดยกมา", "ยอดเงินคงเหลือยกมา", "BALANCE BROUGHT FORWARD"]
    
    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text: continue
            lines = text.split('\n')
            for line in lines:
                line = line.strip()
                if not line: continue

                if any(kw in line.upper() for kw in bf_keywords):
                    amounts = re.findall(r'[\d,]+\.\d{2}', line)
                    if amounts:
                        all_parsed_rows.append([None, None, "B/F", 0.0, 0.0, str_to_float(amounts[-1]), "-", "ยอดยกมา"])
                    continue

                transaction_match = re.match(r'^(\d{2}/\d{2}/\d{2,4})\s+(\d{2}:\d{2})', line)
                if transaction_match:
                    date_str = transaction_match.group(1)
                    time_str = transaction_match.group(2)
                    amounts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', line)
                    
                    parts = line.replace(date_str, "").replace(time_str, "").strip().split()
                    code = parts[0] if len(parts) > 0 else "-"
                    
                    withdraw, deposit, balance = 0.0, 0.0, 0.0
                    if len(amounts) >= 2:
                        balance = str_to_float(amounts[-1])
                        raw_amount = str_to_float(amounts[-2])
                        if code.upper() in ['X1', 'IN', 'IT', 'BT', 'DP', 'CR', 'C1', 'NR']:
                            deposit = raw_amount
                        else:
                            withdraw = raw_amount
                    elif len(amounts) == 1:
                        balance = str_to_float(amounts[0])

                    # หา Channel (เช่น ENET) และรายละเอียด
                    chan = "ENET" if "ENET" in line else "-"
                    desc = line.split(amounts[-1])[-1].strip() if amounts else ""
                    
                    all_parsed_rows.append([date_str, time_str, code, withdraw, deposit, balance, chan, desc])
                
                elif all_parsed_rows and not re.search(r'\d{2}/\d{2}/', line):
                    # เก็บตกรายละเอียดบรรทัดถัดไป
                    all_parsed_rows[-1][7] = (all_parsed_rows[-1][7] + " " + line).strip()

    return all_parsed_rows

# ================= 4. ส่วน UI และการประมวลผล =================

st.title("📑 Bank Statement Converter")

with st.sidebar:
    bank_option = st.selectbox("เลือกธนาคาร", ["กสิกรไทย (KBank)", "ไทยพาณิชย์ (SCB)"])
    pdf_files = st.file_uploader("เลือกไฟล์ PDF", type="pdf", accept_multiple_files=True)
    password = st.text_input("รหัสผ่านไฟล์ (ถ้ามี)", type="password")
    convert_button = st.button("เริ่มการแปลงไฟล์")

if convert_button and pdf_files:
    all_dfs = []
    try:
        for uploaded_file in pdf_files:
            pdf_bytes = uploaded_file.read()
            with pikepdf.open(io.BytesIO(pdf_bytes), password=password) as pdf:
                unlocked_io = io.BytesIO()
                pdf.save(unlocked_io)
                unlocked_io.seek(0)
                
                if bank_option == "กสิกรไทย (KBank)":
                    data = parse_kbank_pdf(unlocked_io)
                    cols = ["วันที่", "เวลา", "รายการ", "ถอนเงิน", "ฝากเงิน", "ยอดคงเหลือ", "รายละเอียดเพิ่มเติม"]
                    date_fmt = '%d-%m-%y'
                else:
                    data = parse_scb_pdf(unlocked_io)
                    cols = ["วันที่", "เวลา", "Code", "ยอดเงินถอน", "ยอดเงินฝาก", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"]
                    date_fmt = '%d/%m/%Y'
                
                df = pd.DataFrame(data, columns=cols)
                df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True, errors='coerce')
                all_dfs.append(df)

        if all_dfs:
            final_df = pd.concat(all_dfs, ignore_index=True)
            st.success(f"แปลงไฟล์ {bank_option} เรียบร้อย")
            st.dataframe(final_df, use_container_width=True)

            # สร้าง Excel แยกตามธนาคาร
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                final_df.to_excel(writer, index=False, sheet_name='Statement')
                workbook = writer.book
                worksheet = writer.sheets['Statement']
                
                num_fmt = workbook.add_format({'num_format': '#,##0.00', 'align': 'right', 'border': 1})
                date_fmt = workbook.add_format({'num_format': 'dd/mm/yyyy', 'border': 1})
                header_fmt = workbook.add_format({'bold': True, 'bg_color': '#EFEFEF', 'border': 1})

                # จัดความกว้างคอลัมน์ตามธนาคาร
                if bank_option == "กสิกรไทย (KBank)":
                    worksheet.set_column('A:A', 12, date_fmt)
                    worksheet.set_column('B:C', 15)
                    worksheet.set_column('D:F', 18, num_fmt)
                    worksheet.set_column('G:G', 50)
                else:
                    worksheet.set_column('A:A', 12, date_fmt)
                    worksheet.set_column('B:C', 10)
                    worksheet.set_column('D:F', 18, num_fmt)
                    worksheet.set_column('G:G', 15)
                    worksheet.set_column('H:H', 50)

            st.download_button(
                label="📥 ดาวน์โหลดไฟล์ Excel",
                data=output.getvalue(),
                file_name=f"Statement_{bank_option}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
    except Exception as e:
        st.error(f"เกิดข้อผิดพลาด: {e}")
