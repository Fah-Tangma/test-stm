import io
import re
import pandas as pd
import pikepdf
import pdfplumber
import streamlit as st
from pikepdf import PasswordError

# ตั้งค่าหน้าเว็บ Streamlit
st.set_page_config(page_title="PDF Statement Converter (Multi-Bank)", layout="wide")

# ================= 1. ฟังก์ชันช่วยเหลือทั่วไป =================
def str_to_float(val_str):
    if val_str is None: return 0.0
    try:
        clean_val = re.sub(r'[^\d.-]', '', str(val_str).replace(',', ''))
        return float(clean_val)
    except:
        return 0.0

# ================= 2. Logic สำหรับกสิกรไทย (KBank) =================
def parse_kbank_content(pdf_stream):
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
                    
                    temp_text = line.replace(date, "", 1).strip()
                    if time: temp_text = temp_text.replace(time, "", 1).strip()
                    desc = temp_text.split(amounts[0])[0].strip() if amounts else temp_text
                    
                    amount_val, balance = 0.0, 0.0
                    if len(amounts) == 1:
                        balance = str_to_float(amounts[0])
                    elif len(amounts) >= 2:
                        # Logic เช็คฝาก/ถอน เบื้องต้น
                        is_deposit = any(kw in desc for kw in ["รับเงิน", "คืนเงิน", "ฝาก", "เงินคืน", "Thai QR", "รับโอน"])
                        val = str_to_float(amounts[0])
                        amount_val = val if is_deposit else -val
                        balance = str_to_float(amounts[-1])
                    
                    # ค้นหา Channel หลังยอดคงเหลือ
                    remaining = line.split(amounts[-1])[-1].strip() if amounts else ""
                    all_parsed_rows.append([date, time, desc, amount_val, balance, "-", remaining])
    return all_parsed_rows

# ================= 3. Logic สำหรับกรุงไทย (KTB) =================
def parse_ktb_content(pdf_stream):
    all_parsed_rows = []
    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            table = page.extract_table({"vertical_strategy": "lines", "horizontal_strategy": "lines"})
            if not table: continue
            for row in table:
                if row[0] and re.search(r'\d{2}/\d{2}/\d{2}', row[0]):
                    dt_parts = row[0].split('\n')
                    date = dt_parts[0].strip()
                    time = dt_parts[1].strip() if len(dt_parts) > 1 else ""
                    desc = row[1].replace('\n', ' ').strip() if row[1] else ""
                    detail = row[2].replace('\n', ' ').strip() if row[2] else ""
                    withdraw = str_to_float(row[3])
                    deposit = str_to_float(row[4])
                    balance = str_to_float(row[5])
                    branch = row[6].strip() if row[6] else ""
                    amount_val = deposit if deposit > 0 else -withdraw
                    all_parsed_rows.append([date, time, desc, amount_val, balance, f"Branch: {branch}", detail])
    return all_parsed_rows

# ================= 4. Logic สำหรับไทยพาณิชย์ (SCB) - ปรับปรุงแก้ไขบรรทัดเพี้ยน =================
def parse_scb_content(pdf_stream):
    all_parsed_rows = []
    bf_keywords = ["ยอดยกมา", "ยอดเงินคงเหลือยกมา", "BALANCE BROUGHT FORWARD"]
    ignore_keywords = ["Date/Time", "Code", "Channel", "Description", "Balance", "หน้าที่", "Page", "เอกสารฉบับนี้"]

    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text: continue
            lines = text.split('\n')
            for line in lines:
                line = line.strip()
                if not line or any(kw in line for kw in ignore_keywords): continue

                # ตรวจสอบยอดยกมา
                if any(kw in line.upper() for kw in bf_keywords):
                    amounts = re.findall(r'[\d,]+\.\d{2}', line)
                    if amounts:
                        all_parsed_rows.append([None, None, "B/F", 0.0, str_to_float(amounts[-1]), "-", "ยอดยกมา"])
                    continue

                # ตรวจสอบรายการหลัก (วันที่ เวลา)
                transaction_match = re.search(r'(\d{2}/\d{2}/\d{2,4})\s+(\d{2}:\d{2})', line)
                if transaction_match:
                    date_str = transaction_match.group(1)
                    time_str = transaction_match.group(2)
                    amounts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', line)
                    
                    # หา Code ธุรกรรม
                    temp_after_time = line.split(time_str)[-1].strip()
                    code = temp_after_time.split()[0] if temp_after_time else "-"
                    
                    amount_val, balance_val = 0.0, 0.0
                    line_desc = ""

                    if amounts:
                        # ป้องกันบรรทัดเพี้ยน: ตัดเอาแค่ข้อความ "ก่อน" ถึงตัวเลขจำนวนเงินตัวแรก
                        first_amt_str = amounts[0]
                        pos_amt = line.find(first_amt_str)
                        pos_code = line.find(code) + len(code)
                        line_desc = line[pos_code:pos_amt].strip()
                        
                        balance_val = str_to_float(amounts[-1])
                        if len(amounts) >= 2:
                            raw_amt = str_to_float(amounts[-2])
                            # เช็ค Code ฝาก/ถอน
                            if code.upper() in ['X1', 'IN', 'IT', 'BT', 'DP', 'CR', 'C1', 'NR', 'QR']:
                                amount_val = raw_amt
                            else:
                                amount_val = -raw_amt

                    channel = "ENET" if "ENET" in line else "-"
                    line_desc = line_desc.replace("ENET", "").strip()
                    
                    all_parsed_rows.append([date_str, time_str, code, amount_val, balance_val, channel, line_desc])
                
                elif all_parsed_rows and not re.search(r'\.\d{2}', line):
                    # เก็บรายละเอียดบรรทัดต่อมา (ถ้าไม่มีตัวเลขจำนวนเงินในบรรทัดนั้น)
                    all_parsed_rows[-1][6] = (all_parsed_rows[-1][6] + " " + line).strip()

    return all_parsed_rows

# ================= 5. ส่วน UI Streamlit =================
st.title("📑 Bank Statement to Excel")
st.info("ระบบรองรับการรวมไฟล์ PDF สูงสุด 5 ไฟล์ (ธนาคารเดียวกัน)")

with st.sidebar:
    st.header("การตั้งค่า")
    bank_option = st.selectbox("เลือกรูปแบบธนาคาร", ["กสิกรไทย (KBank)", "กรุงไทย (KTB)", "ไทยพาณิชย์ (SCB)"])
    st.divider()
    pdf_files = st.file_uploader("เลือกไฟล์ PDF", type="pdf", accept_multiple_files=True)
    password = st.text_input("รหัสผ่านไฟล์ PDF (ถ้ามี)", type="password")
    convert_button = st.button("เริ่มการแปลงไฟล์ทั้งหมด")

if convert_button:
    if pdf_files:
        if len(pdf_files) > 5:
            st.error("❌ กรุณาเลือกไฟล์ไม่เกิน 5 ไฟล์")
        else:
            all_dfs = []
            try:
                progress_bar = st.progress(0)
                for idx, uploaded_file in enumerate(pdf_files):
                    st.write(f"⏳ กำลังประมวลผล: {uploaded_file.name}")
                    pdf_bytes = uploaded_file.read()
                    
                    with pikepdf.open(io.BytesIO(pdf_bytes), password=password) as pdf:
                        unlocked_io = io.BytesIO()
                        pdf.save(unlocked_io)
                        unlocked_io.seek(0)
                        
                        # เลือก Parser ตามธนาคาร
                        if bank_option == "กสิกรไทย (KBank)":
                            rows = parse_kbank_content(unlocked_io)
                            date_p = '%d-%m-%y'
                        elif bank_option == "กรุงไทย (KTB)":
                            rows = parse_ktb_content(unlocked_io)
                            date_p = '%d/%m/%y'
                        else:
                            rows = parse_scb_content(unlocked_io)
                            date_p = '%d/%m/%y'
                        
                        df = pd.DataFrame(rows, columns=["วันที่", "เวลา", "รายการ", "ยอดเงิน", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"])
                        df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True, errors='coerce').dt.date
                        all_dfs.append(df)
                    
                    progress_bar.progress((idx + 1) / len(pdf_files))

                if all_dfs:
                    final_df = pd.concat(all_dfs, ignore_index=True)
                    output = io.BytesIO()
                    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                        final_df.to_excel(writer, index=False, sheet_name='Statement')
                        workbook = writer.book
                        worksheet = writer.sheets['Statement']
                        
                        # Format ตัวเลขและวันที่
                        num_fmt = workbook.add_format({'num_format': '#,##0.00', 'border': 1})
                        date_fmt = workbook.add_format({'num_format': 'dd/mm/yyyy', 'border': 1})
                        header_fmt = workbook.add_format({'bold': True, 'bg_color': '#D7E4BC', 'border': 1})

                        for col_num, value in enumerate(final_df.columns.values):
                            worksheet.write(0, col_num, value, header_fmt)
                        
                        worksheet.set_column('A:A', 12, date_fmt)
                        worksheet.set_column('B:C', 10)
                        worksheet.set_column('D:E', 18, num_fmt)
                        worksheet.set_column('F:F', 15)
                        worksheet.set_column('G:G', 60)
                    
                    st.success("✅ แปลงไฟล์และรวมข้อมูลสำเร็จ!")
                    st.dataframe(final_df, use_container_width=True)
                    st.download_button(
                        label="📥 ดาวน์โหลดไฟล์ Excel รวม", 
                        data=output.getvalue(), 
                        file_name=f"Combined_Statement_{bank_option.split(' ')[0]}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
            except PasswordError:
                st.error("❌ รหัสผ่านไม่ถูกต้อง")
            except Exception as e:
                st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
    else:
        st.warning("⚠️ กรุณาอัปโหลดไฟล์ PDF")
