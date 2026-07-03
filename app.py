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
    if val_str in [None, "", "-", " "]: return 0.0
    try:
        # ลบ comma และช่องว่าง
        clean_val = str(val_str).replace(',', '').strip()
        return float(clean_val)
    except:
        return 0.0

def split_channel_and_detail(text):
    channels = [
        "EDC/K SHOP/MYQR", "โอนเข้า/หักบัญชีอัตโนมัติ", "K PLUS", "ตู้เติมเงิน / โมบาย แอปพลิ", 
        "Internet/Mobile KK", "K BIZ", "EDC", "โอนเข้าหักบัญชีอัตโนมัติ", "ATM", "CDM", 
        "BRANCH", "K-Cash Connect Plus" , "Internet/Mobile GSB", "Internet/Mobile SCB", 
        "Internet/Mobile KTB ", "Internet/Mobile TTB", "ตู้เติมเงิน / โมบาย แอปพลิชัน", "Internet/Mobile BAY"
    ]
    found_channel, detail_part = "-", text
    for c in channels:
        if c in text:
            found_channel = c
            detail_part = text.replace(c, "").strip()
            break
    return found_channel, detail_part

# ================= 2. Logic สำหรับกสิกรไทย (KBank) =================
def parse_kbank_pdf(pdf_stream):
    all_parsed_rows = []
    bf_keywords = ["ยอดยกมา", "Balance Brought Forward", "Brought Forward"]
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
                if not is_in_table or any(kw in line for kw in ["Total", "รวมทั้งสิ้น", "จบรายการ"]):
                    is_in_table = False
                    continue

                date_match = re.match(r'^(\d{2}-\d{2}-\d{2})', line)
                if date_match:
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
                        is_deposit = any(kw in desc for kw in ["รับเงิน", "คืนเงิน", "ฝาก", "เงินคืน", "Thai QR", "รับโอนเงิน"])
                        val = str_to_float(amounts[0])
                        amount_val = val if is_deposit else -val
                        balance = str_to_float(amounts[-1])

                    remaining = ""
                    if amounts:
                        parts = line.split(amounts[-1])
                        if len(parts) > 1: remaining = parts[-1].strip()
                    
                    chan, det = split_channel_and_detail(remaining)
                    # format: [วันที่, เวลา, รายการ, ยอดเงิน, ยอดคงเหลือ, ช่องทาง, รายละเอียด]
                    all_parsed_rows.append([date, time, desc, amount_val, balance, chan, det])

                elif is_in_table:
                    if any(x in line for x in ["หน้า", "แผ่นที่", "ยอดคงเหลือ"]): continue
                    c_extra, d_extra = split_channel_and_detail(line)
                    all_parsed_rows.append(["", "", "", 0.0, 0.0, c_extra if c_extra != "-" else "", d_extra])

    # กรองเอาเฉพาะบรรทัดที่มีข้อมูล (Logic เดิมของ KBank)
    final_rows = [row for row in all_parsed_rows if row[3] != 0 or row[4] != 0 or row[2] != ""]
    return final_rows

# ================= 3. Logic สำหรับไทยพาณิชย์ (SCB) =================
def parse_scb_pdf(pdf_stream):
    all_parsed_rows = []
    bf_keywords = ["ยอดยกมา", "ยอดเงินคงเหลือยกมา", "BALANCE BROUGHT FORWARD"]
    ignore_keywords = [
        "Date/Time", "Code", "Channel", "Cheque No.", "Withdrawal", "Deposit", "Description",
        "Balance Carried Forward", "Total Credit Amount", "Total Debit Amount",
        "จำนวนเงินนำเข้าบัญชีทั้งหมด", "จำนวนเงินที่หักบัญชีทั้งหมด",
        "เอกสารนี้ไม่จำเป็นต้องมีลายเซ็น", "จัดพิมพ์ผ่านระบบคอมพิวเตอร์",
        "สอบถามข้อมูลเพิ่มเติม", "02-722-2222", "Contact Center", "หน้าที่ (Page)", 
        "ช่องทาง", "เลขที่เช็ค", "ยอดเงินหักบัญชี", "ยอดเงินเข้าบัญชี", "รายการ (Items)",
        "ลูกหนี้/เจ้าหนี้", "ยอดเงินคงเหลือ", "TOTAL AMOUNT", "เอกสารฉบับนี้", "TOTAL ITEMS", "This document"
    ]
    
    pending_desc = "" 

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
                        balance = str_to_float(amounts[-1])
                        all_parsed_rows.append([None, None, "ยอดยกมา", 0.0, balance, "-", "BALANCE BROUGHT FORWARD"])
                    continue

                if any(kw in line for kw in ignore_keywords):
                    if "Balance Carried Forward" in line: pending_desc = ""
                    continue

                transaction_match = re.match(r'^(\d{2}/\d{2}/\d{2,4})\s+(\d{2}:\d{2})', line)
                if transaction_match:
                    date_str = transaction_match.group(1)
                    time_str = transaction_match.group(2)
                    amounts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', line)
                    
                    temp_text = line.replace(date_str, "").replace(time_str, "").strip()
                    parts = temp_text.split()
                    code = parts[0] if len(parts) > 0 else "-"
                    
                    channel = "-"
                    if len(parts) > 1 and not re.match(r'[\d,]+\.\d{2}', parts[1]):
                        channel = parts[1]

                    amount_val = 0.0
                    balance_val = 0.0
                    if len(amounts) >= 2:
                        balance_val = str_to_float(amounts[-1])
                        raw_amount = str_to_float(amounts[-2])
                        if code.upper() in ['X1', 'IN', 'IT', 'BT', 'DP', 'CR', 'C1', 'NR']:
                            amount_val = raw_amount
                        else:
                            amount_val = -raw_amount
                    elif len(amounts) == 1:
                        balance_val = str_to_float(amounts[0])

                    line_desc = line.replace(date_str, "").replace(time_str, "").replace(code, "", 1)
                    if channel != "-": line_desc = line_desc.replace(channel, "", 1)
                    for amt in amounts: line_desc = line_desc.replace(amt, "")
                    line_desc = line_desc.strip()

                    final_desc = (pending_desc + " " + line_desc).strip()
                    pending_desc = "" 
                    # format: [วันที่, เวลา, รายการ, ยอดเงิน, ยอดคงเหลือ, ช่องทาง, รายละเอียด]
                    all_parsed_rows.append([date_str, time_str, code, amount_val, balance_val, channel, final_desc])
                
                elif all_parsed_rows:
                    if line.startswith(("รับโอนจาก", "โอนไป", "รับเงินโอน", "ชำระเงิน", "จากระบบ", "ค่าธรรมเนียม")):
                        pending_desc = (pending_desc + " " + line).strip()
                    else:
                        current_desc = all_parsed_rows[-1][6]
                        all_parsed_rows[-1][6] = (current_desc + " " + line).strip()

    return all_parsed_rows

# ================= 4. ส่วนการแสดงผล UI =================

st.title("📑 Bank Statement to Excel Converter")
st.info("รองรับ KBank และ SCB อัปโหลดได้สูงสุด 5 ไฟล์ ระบบจะรวมข้อมูลเข้าด้วยกัน")

with st.sidebar:
    st.header("ตั้งค่าการแปลงไฟล์")
    bank_option = st.selectbox("เลือกรูปแบบธนาคาร", ["กสิกรไทย (KBank)", "ไทยพาณิชย์ (SCB)"])
    st.divider()
    pdf_files = st.file_uploader("เลือกไฟล์ PDF", type="pdf", accept_multiple_files=True)
    password = st.text_input("รหัสผ่านไฟล์ PDF (ถ้ามี)", type="password")
    convert_button = st.button("เริ่มการแปลงไฟล์ทั้งหมด")

if convert_button:
    if pdf_files:
        if len(pdf_files) > 5:
            st.error("❌ กรุณาเลือกไฟล์ไม่เกิน 5 ไฟล์")
        else:
            all_dataframes = []
            try:
                progress_bar = st.progress(0)
                for index, uploaded_file in enumerate(pdf_files):
                    st.write(f"⏳ กำลังประมวลผลไฟล์: {uploaded_file.name}...")
                    pdf_bytes = uploaded_file.read()
                    
                    with pikepdf.open(io.BytesIO(pdf_bytes), password=password) as pdf:
                        unlocked_io = io.BytesIO()
                        pdf.save(unlocked_io)
                        unlocked_io.seek(0)
                        
                        # เลือก Function ตามธนาคาร
                        if bank_option == "กสิกรไทย (KBank)":
                            data_rows = parse_kbank_pdf(unlocked_io)
                            date_format = '%d-%m-%y'
                        else:
                            data_rows = parse_scb_pdf(unlocked_io)
                            date_format = '%d/%m/%Y' # SCB มักใช้ปี 4 หลัก หรือตาม Regex
                        
                        header = ["วันที่", "เวลา", "รายการ", "ยอดเงิน (ฝาก/ถอน)", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"]
                        df_single = pd.DataFrame(data_rows, columns=header)
                        
                        # พยายามแปลงวันที่ให้เป็น datetime เพื่อความสวยงามใน Excel
                        df_single['วันที่'] = pd.to_datetime(df_single['วันที่'], dayfirst=True, errors='coerce')
                        all_dataframes.append(df_single)
                    
                    progress_bar.progress((index + 1) / len(pdf_files))

                if all_dataframes:
                    final_df = pd.concat(all_dataframes, ignore_index=True)
                    
                    output = io.BytesIO()
                    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                        # กำหนดรูปแบบวันที่ใน Excel
                        final_df.to_excel(writer, index=False, sheet_name='Statement')
                        workbook = writer.book
                        worksheet = writer.sheets['Statement']
                        
                        # Accounting format (ตัวเลขมีคอมม่า ยอดลบมีวงเล็บ)
                        num_fmt = workbook.add_format({'num_format': '#,##0.00', 'align': 'right'})
                        date_fmt = workbook.add_format({'num_format': 'dd/mm/yyyy'})

                        worksheet.set_column('A:A', 12, date_fmt)
                        worksheet.set_column('B:C', 12)
                        worksheet.set_column('D:E', 20, num_fmt)
                        worksheet.set_column('F:F', 15)
                        worksheet.set_column('G:G', 60)
                    
                    output.seek(0)
                    st.success(f"✅ แปลงไฟล์สำเร็จ ({len(pdf_files)} ไฟล์)")
                    st.dataframe(final_df, use_container_width=True)
                    st.download_button(
                        label="📥 ดาวน์โหลดไฟล์ Excel รวม", 
                        data=output, 
                        file_name=f"Combined_{bank_option.split(' ')[0]}_Statement.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
            except PasswordError:
                st.error("❌ รหัสผ่านไม่ถูกต้อง")
            except Exception as e:
                st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
    else:
        st.sidebar.warning("⚠️ กรุณาเลือกไฟล์ PDF")
