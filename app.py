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
        # ใช้ regex ลบทุกอย่างที่ไม่ใช่ตัวเลขและจุด
        clean_val = re.sub(r'[^\d.-]', '', str(val_str).replace(',', ''))
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
                    all_parsed_rows.append([date, time, desc, amount_val, balance, chan, det])
                elif is_in_table:
                    if any(x in line for x in ["หน้า", "แผ่นที่", "ยอดคงเหลือ"]): continue
                    c_extra, d_extra = split_channel_and_detail(line)
                    all_parsed_rows.append(["", "", "", 0.0, 0.0, c_extra if c_extra != "-" else "", d_extra])
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

# ================= 4. Logic สำหรับไทยพาณิชย์ (SCB) =================
def parse_scb_content(pdf_stream):
    all_parsed_rows = []
    bf_keywords = ["ยอดยกมา", "ยอดเงินคงเหลือยกมา", "BALANCE BROUGHT FORWARD"]
    # เพิ่มคำที่ควรข้ามเพื่อไม่ให้มันไปเก็บสะสมใน 'รายละเอียด'
    ignore_keywords = [
        "Date/Time", "Code", "Channel", "Description", "Balance", "หน้าที่", "Page",
        "รวมรายการ", "Total Items", "สอบถามข้อมูล", "02-777-7777", "เอกสารฉบับนี้"
    ]

    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text: continue
            
            lines = text.split('\n')
            for line in lines:
                line = line.strip()
                if not line or any(kw in line for kw in ignore_keywords): continue

                # 1. ตรวจสอบยอดยกมา
                if any(kw in line.upper() for kw in bf_keywords):
                    amounts = re.findall(r'[\d,]+\.\d{2}', line)
                    if amounts:
                        all_parsed_rows.append([None, None, "B/F", 0.0, str_to_float(amounts[-1]), "-", "ยอดยกมา"])
                    continue

                # 2. ตรวจสอบบรรทัดรายการหลัก (วันที่ เวลา รหัส ยอดเงิน ยอดคงเหลือ)
                # ใช้ Regex ที่ยืดหยุ่นขึ้นเพื่อตรวจจับ Date และ Time
                transaction_match = re.search(r'(\d{2}/\d{2}/\d{2,4})\s+(\d{2}:\d{2})', line)
                
                if transaction_match:
                    date_str = transaction_match.group(1)
                    time_str = transaction_match.group(2)
                    
                    # ค้นหาตัวเลขทั้งหมด (ยอดเงิน และ ยอดคงเหลือ)
                    amounts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', line)
                    
                    # แยกข้อความที่เหลือ
                    temp_text = line.replace(date_str, "").replace(time_str, "").strip()
                    parts = temp_text.split()
                    code = parts[0] if len(parts) > 0 else "-"
                    
                    amount_val = 0.0
                    balance_val = 0.0
                    
                    # ปกติ SCB รายการหลักต้องมีตัวเลขอย่างน้อย 1-2 ชุด (ยอดเงิน และ ยอดคงเหลือ)
                    if len(amounts) >= 2:
                        balance_val = str_to_float(amounts[-1])
                        raw_amount = str_to_float(amounts[-2])
                        # เช็ค Code ว่าเป็นเงินเข้า (บวก) หรือ เงินออก (ลบ)
                        if code.upper() in ['X1', 'IN', 'IT', 'BT', 'DP', 'CR', 'C1', 'NR', 'QR']:
                            amount_val = raw_amount
                        else:
                            amount_val = -raw_amount
                    elif len(amounts) == 1:
                        balance_val = str_to_float(amounts[0])

                    # --- จุดที่แก้: ตัดเอาเฉพาะรายละเอียดที่อยู่ 'ก่อน' ยอดเงินตัวแรก ---
                    # เพื่อป้องกันไม่ให้ 'รับโอนจาก...' ของบรรทัดถัดไปที่ติดมา ถูกดึงมาด้วย
                    line_desc = ""
                    if amounts:
                        # หาตำแหน่งของยอดเงินตัวแรกในบรรทัด แล้วตัดข้อความแค่ก่อนถึงตรงนั้น
                        first_amt_pos = line.find(amounts[0])
                        # หาตำแหน่งหลัง Code/Channel
                        start_pos = line.find(code) + len(code)
                        line_desc = line[start_pos:first_amt_pos].strip()
                    
                    # ทำความสะอาดรายละเอียด (ลบ Channel ถ้าซ้ำซ้อน)
                    channel = "ENET" if "ENET" in line else "-"
                    line_desc = line_desc.replace("ENET", "").strip()

                    all_parsed_rows.append([date_str, time_str, code, amount_val, balance_val, channel, line_desc])
                
                elif all_parsed_rows:
                    # 3. ถ้าเป็นบรรทัดรายละเอียดที่ไม่มีวันที่ ให้เอาไปต่อท้ายรายการล่าสุด
                    # แต่ต้องไม่ใช่บรรทัดที่มีจำนวนเงิน (เพื่อป้องกันการรวมข้ามรายการ)
                    if not re.search(r'\.\d{2}', line):
                        current_desc = all_parsed_rows[-1][6]
                        all_parsed_rows[-1][6] = (current_desc + " " + line).strip()

    return all_parsed_rows

# ================= 5. ส่วน UI Streamlit =================
st.title("📑 Bank Statement to Excel Converter")

with st.sidebar:
    st.header("ตั้งค่าการแปลงไฟล์")
    bank_option = st.selectbox("เลือกรูปแบบธนาคาร", ["กสิกรไทย (KBank)", "กรุงไทย (KTB)", "ไทยพาณิชย์ (SCB)"])
    st.divider()
    pdf_files = st.file_uploader("เลือกไฟล์ PDF", type="pdf", accept_multiple_files=True)
    password = st.text_input("รหัสผ่านไฟล์ PDF (ถ้ามี)", type="password")
    convert_button = st.button("เริ่มการแปลงไฟล์")

if convert_button:
    if pdf_files:
        all_dfs = []
        try:
            progress_bar = st.progress(0)
            for idx, uploaded_file in enumerate(pdf_files):
                st.write(f"⏳ กำลังอ่าน: {uploaded_file.name}")
                pdf_bytes = uploaded_file.read()
                
                with pikepdf.open(io.BytesIO(pdf_bytes), password=password) as pdf:
                    unlocked_io = io.BytesIO()
                    pdf.save(unlocked_io)
                    unlocked_io.seek(0)
                    
                    if bank_option == "กสิกรไทย (KBank)":
                        rows = parse_kbank_content(unlocked_io)
                        df = pd.DataFrame(rows, columns=["วันที่", "เวลา", "รายการ", "ยอดเงิน", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"])
                        df['วันที่'] = pd.to_datetime(df['วันที่'], format='%d-%m-%y', errors='coerce').dt.date
                    elif bank_option == "กรุงไทย (KTB)":
                        rows = parse_ktb_content(unlocked_io)
                        df = pd.DataFrame(rows, columns=["วันที่", "เวลา", "รายการ", "ยอดเงิน", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"])
                        df['วันที่'] = pd.to_datetime(df['วันที่'], format='%d/%m/%y', errors='coerce').dt.date
                    else: # SCB
                        rows = parse_scb_content(unlocked_io)
                        df = pd.DataFrame(rows, columns=["วันที่", "เวลา", "รายการ", "ยอดเงิน", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"])
                        df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True, errors='coerce').dt.date
                    
                    all_dfs.append(df)
                progress_bar.progress((idx + 1) / len(pdf_files))

            if all_dfs:
                final_df = pd.concat(all_dfs, ignore_index=True)
                
                # สร้างไฟล์ Excel
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                    final_df.to_excel(writer, index=False, sheet_name='Combined')
                    workbook = writer.book
                    worksheet = writer.sheets['Combined']
                    
                    # Formatting
                    header_fmt = workbook.add_format({'bold': True, 'bg_color': '#D7E4BC', 'border': 1})
                    num_fmt = workbook.add_format({'num_format': '#,##0.00', 'border': 1})
                    date_fmt = workbook.add_format({'num_format': 'dd/mm/yyyy', 'border': 1})
                    text_fmt = workbook.add_format({'border': 1})

                    for col_num, value in enumerate(final_df.columns.values):
                        worksheet.write(0, col_num, value, header_fmt)
                    
                    worksheet.set_column('A:A', 12, date_fmt)
                    worksheet.set_column('B:C', 12, text_fmt)
                    worksheet.set_column('D:E', 18, num_fmt)
                    worksheet.set_column('F:F', 15, text_fmt)
                    worksheet.set_column('G:G', 60, text_fmt)

                st.success("✅ แปลงไฟล์สำเร็จ!")
                st.dataframe(final_df, use_container_width=True)
                st.download_button(
                    label="📥 ดาวน์โหลดไฟล์ Excel",
                    data=output.getvalue(),
                    file_name=f"Statement_{bank_option.split(' ')[0]}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
        except PasswordError:
            st.error("❌ รหัสผ่าน PDF ไม่ถูกต้อง")
        except Exception as e:
            st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
    else:
        st.warning("⚠️ กรุณาอัปโหลดไฟล์ PDF ก่อน")
