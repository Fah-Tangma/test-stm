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
    # Keyword สำหรับยอดยกมา
    bf_keywords = ["ยอดยกมา", "ยอดเงินคงเหลือยกมา", "BALANCE BROUGHT FORWARD"]
    
    # รายการคำที่ต้องข้าม (Ignore)
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

                # --- แก้ไขจุดสำคัญที่ 1: ตรวจสอบยอดยกมาก่อนจะทำการ Ignore ---
                # เพราะคำว่า "ยอดเงินคงเหลือยกมา" มีคำว่า "ยอดเงินคงเหลือ" ซึ่งอยู่ใน list ignore
                if any(kw in line.upper() for kw in bf_keywords):
                    amounts = re.findall(r'[\d,]+\.\d{2}', line)
                    if amounts:
                        balance = str_to_float(amounts[-1])
                        # ใช้ None ในช่องวันที่เพื่อให้ Pandas จัดการเป็น NaT (ช่องว่าง)
                        all_parsed_rows.append([None, None, "B/F", "-", 0.0, balance, "ยอดยกมา (BALANCE BROUGHT FORWARD)"])
                    continue

                # --- กรองบรรทัดที่ไม่เกี่ยวข้องอื่นๆ ---
                if any(kw in line for kw in ignore_keywords):
                    if "Balance Carried Forward" in line: pending_desc = ""
                    continue

                # 3. ตรวจสอบบรรทัดรายการหลัก (รองรับทั้งปี 2 หลัก และ 4 หลัก)
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
                        
                        # ตรวจสอบ Code ฝาก/ถอน
                        if code.upper() in ['X1', 'IN', 'IT', 'BT', 'DP', 'CR', 'C1', 'NR']:
                            amount_val = raw_amount
                        else:
                            # BPAY, X2, TX, WD ฯลฯ ให้เป็นค่าลบ
                            amount_val = -raw_amount
                    elif len(amounts) == 1:
                        balance_val = str_to_float(amounts[0])

                    line_desc = line.replace(date_str, "").replace(time_str, "").replace(code, "", 1)
                    if channel != "-": line_desc = line_desc.replace(channel, "", 1)
                    for amt in amounts: line_desc = line_desc.replace(amt, "")
                    line_desc = line_desc.strip()

                    final_desc = (pending_desc + " " + line_desc).strip()
                    pending_desc = "" 

                    all_parsed_rows.append([date_str, time_str, code, channel, amount_val, balance_val, final_desc])
                
                elif all_parsed_rows:
                    # เก็บตกรายละเอียดบรรทัดถัดไป
                    if line.startswith(("รับโอนจาก", "โอนไป", "รับเงินโอน", "ชำระเงิน", "จากระบบ", "ค่าธรรมเนียม")):
                        pending_desc = (pending_desc + " " + line).strip()
                    else:
                        current_desc = all_parsed_rows[-1][6]
                        all_parsed_rows[-1][6] = (current_desc + " " + line).strip()

    return all_parsed_rows

# ================= 5. ส่วน UI Streamlit =================
st.title("📑 PDF Statement to Excel")
st.info("อัพโหลดไฟล์ PDF ได้สูงสุด 5 ไฟล์ ระบบจะรวมข้อมูลเข้าด้วยกันตามลำดับการเลือกไฟล์")

with st.sidebar:
    st.header("Statement to Excel")
    bank_option = st.selectbox("เลือกรูปแบบธนาคาร", ["กสิกรไทย (KBank)"])
    st.divider()
    # ปรับให้อัพโหลดได้หลายไฟล์
    pdf_files = st.file_uploader("เลือกไฟล์ PDF (สูงสุด 5 ไฟล์)", type="pdf", accept_multiple_files=True)
    password = st.text_input("รหัสผ่านไฟล์ PDF (ถ้ามี)", type="password")
    convert_button = st.button("เริ่มการแปลงไฟล์ทั้งหมด")

if convert_button:
    if pdf_files:
        if len(pdf_files) > 5:
            st.error("❌ กรุณาเลือกไฟล์ไม่เกิน 5 ไฟล์")
        else:
            all_dataframes = []
            status_container = st.container()
            
            try:
                progress_bar = st.progress(0)
                for index, uploaded_file in enumerate(pdf_files):
                    status_container.write(f"⏳ กำลังประมวลผลไฟล์: {uploaded_file.name}...")
                    
                    pdf_bytes = uploaded_file.read()
                    with pikepdf.open(io.BytesIO(pdf_bytes), password=password) as pdf:
                        unlocked_io = io.BytesIO()
                        pdf.save(unlocked_io)
                        unlocked_io.seek(0)
                        
                        data_rows = parse_pdf_content(unlocked_io)
                        header = ["วันที่", "เวลา", "รายการ", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"]
                        
                        df_single = pd.DataFrame(data_rows, columns=header)
                        # แปลงวันที่เพื่อให้เรียงลำดับได้ หรือรักษา Format
                        df_single['วันที่'] = pd.to_datetime(df_single['วันที่'], format='%d-%m-%y', errors='coerce')
                        
                        all_dataframes.append(df_single)
                    
                    progress_bar.progress((index + 1) / len(pdf_files))

                if all_dataframes:
                    # รวม DataFrame ทั้งหมดเข้าด้วยกัน
                    final_df = pd.concat(all_dataframes, ignore_index=True)
                    
                    # (ทางเลือก) เรียงลำดับตามวันที่ถ้าต้องการ
                    # final_df = final_df.sort_values(by=['วันที่', 'เวลา']).reset_index(drop=True)

                    output = io.BytesIO()
                    with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='m/d/yyyy') as writer:
                        final_df.to_excel(writer, index=False, sheet_name='Combined_Statement')
                        workbook = writer.book
                        worksheet = writer.sheets['Combined_Statement']
                        
                        num_fmt = workbook.add_format({
                            'num_format': '_(* #,##0.00_);_(* (#,##0.00);_(* "-"??_);_(@_)',
                            'align': 'right'
                        })
                        date_fmt = workbook.add_format({'num_format': 'm/d/yyyy', 'align': 'left'})

                        worksheet.set_column('A:A', 15, date_fmt)
                        worksheet.set_column('B:B', 10)
                        worksheet.set_column('C:C', 20)
                        worksheet.set_column('D:E', 20, num_fmt)
                        worksheet.set_column('F:F', 20)
                        worksheet.set_column('G:G', 50)
                    
                    output.seek(0)
                    st.success(f"✅ รวมไฟล์สำเร็จ ({len(pdf_files)} ไฟล์)")
                    st.dataframe(final_df, use_container_width=True)
                    st.download_button(
                        label="📥 ดาวน์โหลดไฟล์ Excel รวม", 
                        data=output, 
                        file_name="Combined_Statement.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
            except PasswordError:
                st.error("❌ รหัสผ่านไม่ถูกต้อง หรือไฟล์บางไฟล์ต้องการรหัสผ่านที่ต่างกัน")
            except Exception as e:
                st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
    else:
        st.sidebar.warning("⚠️ กรุณาเลือกไฟล์ PDF อย่างน้อย 1 ไฟล์")
