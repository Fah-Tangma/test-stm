import io
import re
import pandas as pd
import pikepdf
import pdfplumber
import streamlit as st
from pikepdf import PasswordError
from datetime import datetime

# ตั้งค่าหน้าเว็บ Streamlit
st.set_page_config(page_title="PDF Statement Converter", layout="wide")

# ================= 1. ฟังก์ชันช่วยเหลือ (Common Helpers) =================
def str_to_float(val_str):
    if val_str in [None, "", "-", " "]: return None
    try:
        clean_val = re.sub(r'[^\d.-]', '', str(val_str))
        return float(clean_val)
    except:
        return None

def decode_cid(text):
    """แปลงรหัส CID เป็นตัวเลขสำหรับ KTB"""
    if not text: return ""
    cid_map = {
        "(cid:344)": "0", "(cid:345)": "1", "(cid:346)": "2", "(cid:347)": "3", "(cid:348)": "4",
        "(cid:349)": "5", "(cid:350)": "6", "(cid:351)": "7", "(cid:352)": "8", "(cid:353)": "9",
    }
    for cid, val in cid_map.items():
        text = text.replace(cid, val)
    return text

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

    # กรองข้อมูล
    temp_list_bf = []
    found_first_bf = False
    for row in all_parsed_rows:
        if any(kw in str(row[2]) for kw in bf_keywords):
            if not found_first_bf:
                temp_list_bf.append(row)
                found_first_bf = True
        else:
            temp_list_bf.append(row)

    final_filtered_rows = []
    i, n = 0, len(temp_list_bf)
    while i < n:
        if temp_list_bf[i][3] is not None or any(kw in str(temp_list_bf[i][2]) for kw in bf_keywords):
            final_filtered_rows.append(temp_list_bf[i])
            i += 1
        else:
            empty_block = []
            while i < n and temp_list_bf[i][3] is None and not any(kw in str(temp_list_bf[i][2]) for kw in bf_keywords):
                empty_block.append(temp_list_bf[i])
                i += 1
            if len(empty_block) == 1:
                final_filtered_rows.append(empty_block[0])
            
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

# ================= 4. Logic สำหรับ KTB =================
def parse_ktb_pdf(pdf_stream):
    all_data = []
    ignore_keywords = ["ธนาคารกรุงไทย", "หน้า", "รายการเดินบัญชี", "ชื่อบัญชี", "ประเภทบัญชี",
        "เลขที่บัญชี", "รหัสสาขา", "ที่อยู่ปัจจุบัน", "ที่อยู่สาขา", "วงเงินเบิกเกินบัญชี",
        "สกุลเงิน", "ติดต่อ เบอร์", "อีเมล", "Krungthai Bank", "Statement", 
        "ยอดคงเหลือยกมา", "รวมรายการ", "เลขที่", "บริษัท ธนาคารกรุงไทย",
        "ถนนสุขุมวิท", "แขวงคลองเตยเหนือ", "เขตวัฒนา", "กรุงเทพฯ", 
        "Krungthai Corporate Call Center", "02-111-9999", 
        "cash.management@krungthai.com", "www.krungthai.com"]
    
    deposit_codes = ['IORSDT', 'IIPS', 'DDSDT', 'CR', 'OTHDEP']

    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text: continue
            text = decode_cid(text)
            lines = text.split('\n')
            last_entry_idx = -1

            for line in lines:
                line = line.strip()
                if not line or any(keyword in line for keyword in ignore_keywords): continue

                # รูปแบบ Biz (YYYY)
                biz_match = re.match(r'^(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})\s+(\w+)\s+(.*)', line)
                if biz_match:
                    date_str, time_val, transaction, rem_text = biz_match.groups()
                    amounts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', rem_text)
                    if len(amounts) >= 2:
                        amount_val = str_to_float(amounts[0])
                        balance_val = str_to_float(amounts[-1])
                        final_amount = amount_val if ('DT' in transaction or 'CR' in transaction) else -amount_val
                        branch = rem_text.split(amounts[-1])[-1].strip() or "Krungthai Business"
                        all_data.append([date_str, time_val, transaction, rem_text.split(amounts[0])[0].strip(), final_amount, balance_val, branch])
                        last_entry_idx = len(all_data) - 1
                        continue

                # รูปแบบบุคคล (YY)
                main_match = re.match(r'^(\d{2}/\d{2}/\d{2})\s*(.*?)\s*\(([A-Z]+)\)\s*(.*)', line)
                if main_match:
                    date_str, trans_name, code, rem_text = main_match.groups()
                    amounts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', rem_text)
                    if len(amounts) >= 2:
                        raw_val = str_to_float(amounts[1]) if len(amounts) >= 3 and str_to_float(amounts[0]) == 0 else str_to_float(amounts[0])
                        balance_val = str_to_float(amounts[-1])
                        final_amount = raw_val if (code in deposit_codes or "เข้า" in trans_name) else -raw_val
                        all_data.append([date_str, "", f"{trans_name} ({code})", rem_text.split(amounts[0])[0].strip(), final_amount, balance_val, line.split()[-1]])
                        last_entry_idx = len(all_data) - 1
                        continue

                # เก็บเวลาและรายละเอียดเพิ่มเติม
                time_row_match = re.match(r'^(\d{2}:\d{2})(.*)', line)
                if time_row_match and last_entry_idx != -1:
                    all_data[last_entry_idx][1] = time_row_match.group(1)
                    if time_row_match.group(2): all_data[last_entry_idx][3] += " " + time_row_match.group(2).strip()
                elif last_entry_idx != -1 and not re.match(r'^\d{2}/\d{2}/', line) and "C/F" not in line:
                    all_data[last_entry_idx][3] += " " + line
    return all_data

# ================= 5. Streamlit UI & Export =================
st.title("📑 PDF Statement to Excel")

with st.sidebar:
    st.header("ตัวเลือก")
    bank_option = st.selectbox("เลือกธนาคาร", ["กสิกรไทย (KBank)", "ไทยพาณิชย์ (SCB)", "กรุงไทย (KTB)"])
    pdf_files = st.file_uploader("เลือกไฟล์ PDF", type="pdf", accept_multiple_files=True)
    password = st.text_input("รหัสผ่านไฟล์ (ถ้ามี)", type="password")
    convert_button = st.button("เริ่มการแปลงไฟล์")

if convert_button:
    if not pdf_files:
        st.error("⚠️ กรุณาเลือกไฟล์ PDF")
    else:
        all_dfs = []
        try:
            for uploaded_file in pdf_files:
                pdf_bytes = uploaded_file.read()
                with pikepdf.open(io.BytesIO(pdf_bytes), password=password) as pdf:
                    unlocked_io = io.BytesIO()
                    pdf.save(unlocked_io)
                    unlocked_io.seek(0)
                    
                    if bank_option == "กสิกรไทย (KBank)":
                        rows = parse_kbank_pdf(unlocked_io)
                        df = pd.DataFrame(rows, columns=["วันที่", "เวลา", "รายการ", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"])
                        df['วันที่'] = pd.to_datetime(df['วันที่'], format='%d-%m-%y', errors='coerce')
                    elif bank_option == "ไทยพาณิชย์ (SCB)":
                        rows = parse_scb_pdf(unlocked_io)
                        df = pd.DataFrame(rows, columns=["วันที่", "เวลา", "Code", "ช่องทาง", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "รายละเอียด"])
                        df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True, errors='coerce')
                    else: # KTB
                        rows = parse_ktb_pdf(unlocked_io)
                        df = pd.DataFrame(rows, columns=["วันที่", "เวลา", "รายการ", "รายละเอียด", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "สาขา"])
                        df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True, errors='coerce')
                    all_dfs.append(df)

            if all_dfs:
                final_df = pd.concat(all_dfs, ignore_index=True)
                
                # แสดงผลในหน้าเว็บ
                st.dataframe(final_df, use_container_width=True)

                # สร้าง Excel
                output = io.BytesIO()
                # กำหนด datetime_format='m/d/yyyy' ตรงนี้เพื่อให้ Excel แสดงผลตามต้องการ
                with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='m/d/yyyy') as writer:
                    final_df.to_excel(writer, index=False, sheet_name='Statement')
                    workbook = writer.book
                    worksheet = writer.sheets['Statement']

                    # ตั้งค่าสีหัวตาราง
                    colors = {"กสิกรไทย (KBank)": '#00A950', "ไทยพาณิชย์ (SCB)": '#4E2E7F', "กรุงไทย (KTB)": '#00A1E0'}
                    h_color = colors.get(bank_option, '#333333')
                    
                    header_fmt = workbook.add_format({'bold': True, 'bg_color': h_color, 'font_color': 'white', 'align': 'center'})
                    num_fmt = workbook.add_format({'num_format': '#,##0.00', 'align': 'right'})
                    # กำหนดฟอร์แมตวันที่สำหรับคอลัมน์ A (วันที่)
                    date_fmt = workbook.add_format({'num_format': 'm/d/yyyy', 'align': 'left'})

                    for col_num, value in enumerate(final_df.columns.values):
                        worksheet.write(0, col_num, value, header_fmt)
                    
                    # บังคับใช้ Date Format ในคอลัมน์แรก
                    worksheet.set_column('A:A', 15, date_fmt)
                    # ตั้งค่าตัวเลขในคอลัมน์จำนวนเงินและยอดคงเหลือ
                    if bank_option == "กสิกรไทย (KBank)":
                        worksheet.set_column('D:E', 15, num_fmt)
                    else:
                        worksheet.set_column('E:F', 15, num_fmt)

                output.seek(0)
                dl_name = f"Statement_{bank_option}_{datetime.now().strftime('%Y%m%d')}.xlsx"
                st.download_button(label="📥 ดาวน์โหลดไฟล์ Excel", data=output, file_name=dl_name)

        except PasswordError:
            st.error("❌ รหัสผ่านไม่ถูกต้อง")
        except Exception as e:
            st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
