import io
import re
import pandas as pd
import pikepdf
import pdfplumber
import streamlit as st
from pikepdf import PasswordError

# ตั้งค่าหน้าเว็บ Streamlit
st.set_page_config(page_title="PDF Statement Converter", layout="wide")

# ================= 1. ฟังก์ชันช่วยเหลือ (Common) =================
def str_to_float(val_str):
    if val_str in [None, "", "-", " "]: return None
    try:
        clean_val = re.sub(r'[^\d.-]', '', str(val_str))
        return float(clean_val)
    except:
        return None

def split_channel_and_detail(text):
    channels = [
        "EDC/K SHOP/MYQR", "โอนเข้า/หักบัญชีอัตโนมัติ", "K PLUS", "ตู้เติมเงิน / โมบาย แอปพลิ", 
        "Internet/Mobile KK", "K BIZ", "EDC", "โอนเข้าหักบัญชีอัตโนมัติ", "ATM", "CDM", 
        "BRANCH", "K-Cash Connect Plus" , "Internet/Mobile GSB", "Internet/Mobile SCB", 
        "Internet/Mobile KTB ", "Internet/Mobile TTB", "ตู้เติมเงิน / โมบาย แอปพลิชัน", "Internet/Mobile BAY", 
        "Internet/Mobile BBL","Internet/Mobile BAAC", "สาขาถนนศรีสุริยวงศ์", "สาขาเซ็นทรัล ขอนแก่น"
    ]
    found_channel, detail_part = "-", text
    for c in channels:
        if c in text:
            found_channel = c
            detail_part = text.replace(c, "").strip()
            break
    return found_channel, detail_part

# ================= 2. Logic สำหรับ KBank =================
def parse_kbank_pdf(pdf_stream):
    all_parsed_rows = []
    bf_keywords = ["ยอดยกมา", "Balance Brought Forward", "Brought Forward"]
    table_headers = ["เวลา/", "วันที่มีผล", "ถอนเงิน / ฝากเงิน", "ยอดคงเหลือ"]

    with pdfplumber.open(pdf_stream) as pdf_obj:
        for page in pdf_obj.pages:
            text = page.extract_text()
            if not text: continue
            lines = text.split('\n')
            is_in_table = False 

            for line in lines:
                line = line.strip()
                if not line: continue
                
                # --- 1. เช็ควันทีก่อน (ลำดับสำคัญ เพื่อไม่ให้ข้ามรายการ 'ถอนเงินสด') ---
                date_match = re.match(r'^(\d{2}-\d{2}-\d{2})', line)
                
                if date_match:
                    is_in_table = True 
                    date = date_match.group(1)
                    time_match = re.search(r'(\d{2}:\d{2})', line)
                    time = time_match.group(1) if time_match else ""
                    
                    amounts = re.findall(r'[\d,]+\.\d{2}', line)
                    temp_text = line.replace(date, "", 1).strip()
                    if time: temp_text = temp_text.replace(time, "", 1).strip()
                    
                    desc = temp_text.split(amounts[0])[0].strip() if amounts else temp_text
                    
                    amount_val, balance = None, None
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
                    continue

                if any(kw in line for kw in table_headers):
                    is_in_table = True
                    continue
                
                if any(kw in line for kw in ["Total", "รวมทั้งสิ้น", "จบรายการ"]):
                    is_in_table = False
                    continue

                if is_in_table:
                    if any(x in line for x in ["หน้า", "แผ่นที่", "ยอดคงเหลือ"]): continue
                    c_extra, d_extra = split_channel_and_detail(line)
                    all_parsed_rows.append(["", "", "", None, None, c_extra if c_extra != "-" else "", d_extra])

    # --- Logic การกรองข้อมูลตามเงื่อนไขใหม่ ---
    
    # ขั้นตอนที่ 1: ลบ "ยอดยกมา" นอกจากตัวแรกสุด
    temp_list_bf = []
    found_first_bf = False
    for row in all_parsed_rows:
        is_bf_row = any(kw in str(row[2]) for kw in bf_keywords)
        if is_bf_row:
            if not found_first_bf:
                temp_list_bf.append(row)
                found_first_bf = True
            # ถ้าเป็น B/F ตัวที่ 2 เป็นต้นไป จะไม่ถูกเพิ่มเข้า list (คือการลบนั่นเอง)
        else:
            temp_list_bf.append(row)

    # ขั้นตอนที่ 2: ลบแถวว่างติดต่อกันมากกว่า 1 แถวออกทั้งหมด
    final_filtered_rows = []
    i, n = 0, len(temp_list_bf)
    while i < n:
        # แถวที่มีจำนวนเงิน หรือเป็นยอดยกมาตัวแรก ให้เก็บไว้ทันที
        if temp_list_bf[i][3] is not None or any(kw in str(temp_list_bf[i][2]) for kw in bf_keywords):
            final_filtered_rows.append(temp_list_bf[i])
            i += 1
        else:
            # เริ่มตรวจสอบกลุ่มแถวว่าง (Amount is None)
            empty_block = []
            while i < n and temp_list_bf[i][3] is None and not any(kw in str(temp_list_bf[i][2]) for kw in bf_keywords):
                empty_block.append(temp_list_bf[i])
                i += 1
            
            # ถ้ากลุ่มแถวว่างมีแค่ 1 แถว ให้เก็บไว้ (อาจเป็นรายละเอียด)
            if len(empty_block) == 1:
                final_filtered_rows.append(empty_block[0])
            # ถ้ามีมากกว่า 1 แถว เข้าเงื่อนไข "ลบทั้งหมด" (ไม่ต้อง append อะไรเลย)
            
    return final_filtered_rows

# ================= 3. Logic สำหรับ SCB =================
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
                        all_parsed_rows.append([None, None, "B/F", "-", 0.0, balance, "ยอดยกมา (BALANCE BROUGHT FORWARD)"])
                    continue
                if any(kw in line for kw in ignore_keywords): continue
                transaction_match = re.match(r'^(\d{2}/\d{2}/\d{2,4})\s+(\d{2}:\d{2})', line)
                if transaction_match:
                    date_str = transaction_match.group(1)
                    time_str = transaction_match.group(2)
                    amounts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', line)
                    temp_text = line.replace(date_str, "").replace(time_str, "").strip()
                    parts = temp_text.split()
                    code = parts[0] if len(parts) > 0 else "-"
                    channel = parts[1] if len(parts) > 1 and not re.match(r'[\d,]+\.\d{2}', parts[1]) else "-"
                    amount_val, balance_val = 0.0, 0.0
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
                    final_desc = (pending_desc + " " + line_desc.strip()).strip()
                    pending_desc = ""
                    all_parsed_rows.append([date_str, time_str, code, channel, amount_val, balance_val, final_desc])
                elif all_parsed_rows:
                    if line.startswith(("รับโอนจาก", "โอนไป", "รับเงินโอน", "ชำระเงิน", "จากระบบ", "ค่าธรรมเนียม")):
                        pending_desc = (pending_desc + " " + line).strip()
                    else:
                        all_parsed_rows[-1][6] = (all_parsed_rows[-1][6] + " " + line).strip()
    return all_parsed_rows

# ================= 4. Streamlit UI & Excel Export =================

st.title("📑 PDF Statement to Excel")

# ใช้ Placeholder สำหรับ Info เพื่อให้สั่งลบได้ในภายหลังถ้าต้องการ
info_placeholder = st.empty()
info_placeholder.info("อัพโหลดไฟล์ PDF ได้สูงสุด 5 ไฟล์ ระบบจะรวมข้อมูลเข้าด้วยกันตามลำดับการเลือกไฟล์")

with st.sidebar:
    st.header("ตัวเลือก")
    bank_option = st.selectbox("เลือกธนาคาร", ["กสิกรไทย (KBank)", "ไทยพาณิชย์ (SCB)"])
    pdf_files = st.file_uploader("เลือกไฟล์ PDF (สูงสุด 5 ไฟล์)", type="pdf", accept_multiple_files=True)
    password = st.text_input("รหัสผ่านไฟล์ (ถ้ามี)", type="password")
    convert_button = st.button("เริ่มการแปลงไฟล์ทั้งหมด")

if convert_button:
    if not pdf_files:
        st.error("⚠️ กรุณาเลือกไฟล์ PDF")
    else:
        # --- สร้าง Placeholder สำหรับ แถบสถานะต่างๆ ---
        status_placeholder = st.empty()   # สำหรับ "กำลังประมวลผล..."
        progress_placeholder = st.empty() # สำหรับ Progress Bar
        success_placeholder = st.empty()  # สำหรับ แถบสีเขียว
        
        all_dfs = []
        try:
            for i, uploaded_file in enumerate(pdf_files):
                # แสดงสถานะปัจจุบัน
                status_placeholder.write(f"⏳ กำลังประมวลผลไฟล์: {uploaded_file.name}...")
                progress_placeholder.progress((i + 1) / len(pdf_files))
                
                pdf_bytes = uploaded_file.read()
                with pikepdf.open(io.BytesIO(pdf_bytes), password=password) as pdf:
                    unlocked_io = io.BytesIO()
                    pdf.save(unlocked_io)
                    unlocked_io.seek(0)
                    
                    if bank_option == "กสิกรไทย (KBank)":
                        rows = parse_kbank_pdf(unlocked_io)
                        cols = ["วันที่", "เวลา", "รายการ", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"]
                        df = pd.DataFrame(rows, columns=cols)
                        df['วันที่'] = pd.to_datetime(df['วันที่'], format='%d-%m-%y', errors='coerce')
                    else:
                        rows = parse_scb_pdf(unlocked_io)
                        cols = ["วันที่", "เวลา", "Code", "ช่องทาง", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "รายละเอียด"]
                        df = pd.DataFrame(rows, columns=cols)
                        df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True, errors='coerce')
                    all_dfs.append(df)

            if all_dfs:
                final_df = pd.concat(all_dfs, ignore_index=True)

                # --- เคลียร์แถบสถานะทิ้ง (ให้หายไปจากหน้าจอ) ---
                status_placeholder.empty()
                progress_placeholder.empty()
                # info_placeholder.empty() # ถ้าต้องการให้ Info ด้านบนหายไปด้วย ให้เอาคอมเม้นต์ออก
                
                # แสดงตารางผลลัพธ์
                st.dataframe(final_df, use_container_width=True)

                # สร้าง Excel
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='m/d/yyyy') as writer:
                    sheet_name = 'Statement'
                    final_df.to_excel(writer, index=False, sheet_name=sheet_name)
                    workbook = writer.book
                    worksheet = writer.sheets[sheet_name]

                    is_scb = (bank_option == "ไทยพาณิชย์ (SCB)")
                    header_color = '#4E2E7F' if is_scb else '#00A950'
                    num_cols_range = 'E:F' if is_scb else 'D:E'

                    header_fmt = workbook.add_format({'bold': True, 'bg_color': header_color, 'font_color': 'white', 'border': 0, 'align': 'center', 'valign': 'vcenter'})
                    num_fmt = workbook.add_format({'num_format': '_(* #,##0.00_);_(* (#,##0.00);_(* "-"??_);_(@_)', 'align': 'right', 'border': 0, 'valign': 'vcenter'})
                    date_fmt = workbook.add_format({'num_format': 'm/d/yyyy', 'align': 'left', 'border': 0, 'valign': 'vcenter'})
                    text_fmt = workbook.add_format({'border': 0, 'valign': 'vcenter'})

                    for col_num, value in enumerate(final_df.columns.values):
                        worksheet.write(0, col_num, value, header_fmt)

                    worksheet.set_column('A:A', 15, date_fmt)
                    worksheet.set_column('B:D', 10, text_fmt)
                    worksheet.set_column(num_cols_range, 20, num_fmt)
                    worksheet.set_column('G:G', 80, text_fmt)

                output.seek(0)
                st.download_button(
                    label=f"📥 ดาวน์โหลดไฟล์ Excel ({bank_option})",
                    data=output,
                    file_name=f"Combined_Statement.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
                
                # แสดงความสำเร็จแวบเดียว หรือไม่แสดงเลยตามใจคนใช้
                # success_placeholder.success(f"✅ รวมไฟล์สำเร็จ ({len(pdf_files)} ไฟล์)")

        except PasswordError:
            status_placeholder.empty()
            progress_placeholder.empty()
            st.error("❌ รหัสผ่านไม่ถูกต้อง")
        except Exception as e:
            status_placeholder.empty()
            progress_placeholder.empty()
            st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
