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
    if val_str in [None, "", "-", " "]: return 0.0
    try:
        clean_val = re.sub(r'[^\d.-]', '', str(val_str))
        return float(clean_val)
    except:
        return 0.0

# ================= 2. Logic สำหรับ KBank =================
def split_channel_and_detail_kbank(text):
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
                    chan, det = split_channel_and_detail_kbank(remaining)
                    all_parsed_rows.append([date, time, desc, amount_val, balance, chan, det])
                elif is_in_table:
                    if any(x in line for x in ["หน้า", "แผ่นที่", "ยอดคงเหลือ"]): continue
                    c_extra, d_extra = split_channel_and_detail_kbank(line)
                    all_parsed_rows.append(["", "", "", None, None, c_extra if c_extra != "-" else "", d_extra])
    return all_parsed_rows

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

with st.sidebar:
    st.header("ตัวเลือก")
    bank_option = st.selectbox("เลือกธนาคาร", ["กสิกรไทย (KBank)", "ไทยพาณิชย์ (SCB)"])
    pdf_files = st.file_uploader("เลือกไฟล์ PDF (สูงสุด 5 ไฟล์)", type="pdf", accept_multiple_files=True)
    password = st.text_input("รหัสผ่านไฟล์ (ถ้ามี)", type="password")
    convert_button = st.button("ประมวลผลไฟล์ทั้งหมด")

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
                        cols = ["วันที่", "เวลา", "รายการ", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"]
                        df = pd.DataFrame(rows, columns=cols)
                        df['วันที่'] = pd.to_datetime(df['วันที่'], format='%d-%m-%y', errors='coerce')
                    else:
                        rows = parse_scb_pdf(unlocked_io)
                        cols = ["วันที่", "เวลา", "Code", "ช่องทาง", "ยอดเงิน (ฝาก/ถอน)", "ยอดคงเหลือ", "รายละเอียด"]
                        df = pd.DataFrame(rows, columns=cols)
                        df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True, errors='coerce')
                    
                    all_dfs.append(df)

            if all_dfs:
                final_df = pd.concat(all_dfs, ignore_index=True)
                
                # แสดงผลในหน้าเว็บ
                st.dataframe(final_df, use_container_width=True)

                output = io.BytesIO()
                # --- จุดสำคัญ: เพิ่ม datetime_format='m/d/yyyy' ใน ExcelWriter ---
                with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='m/d/yyyy') as writer:
                    sheet_name = 'Statement'
                    final_df.to_excel(writer, index=False, sheet_name=sheet_name)
                    workbook = writer.book
                    worksheet = writer.sheets[sheet_name]

                    # ตั้งค่าพื้นฐาน (ไม่มีเส้นขอบ)
                    is_scb = (bank_option == "ไทยพาณิชย์ (SCB)")
                    border_val = 0 
                    header_color = '#4E2E7F' if is_scb else '#00A950'
                    num_cols_range = 'E:F' if is_scb else 'D:E'

                    # 1. Format สำหรับหัวตาราง
                    header_fmt = workbook.add_format({
                        'bold': True, 'bg_color': header_color, 'font_color': 'white', 
                        'border': border_val, 'align': 'center', 'valign': 'vcenter'
                    })

                    # 2. Format สำหรับตัวเลข (Accounting)
                    num_fmt = workbook.add_format({
                        'num_format': '_(* #,##0.00_);_(* (#,##0.00);_(* "-"??_);_(@_)',
                        'align': 'right',
                        'border': border_val,
                        'valign': 'vcenter'
                    })

                    # 3. Format สำหรับวันที่ (m/d/yyyy) - บังคับ Format อีกครั้งที่คอลัมน์ A
                    date_fmt = workbook.add_format({
                        'num_format': 'm/d/yyyy', 
                        'align': 'left',
                        'border': border_val,
                        'valign': 'vcenter'
                    })

                    # 4. Format สำหรับข้อความทั่วไป
                    text_fmt = workbook.add_format({
                        'border': border_val, 
                        'valign': 'vcenter'
                    })

                    # เขียน Header
                    for col_num, value in enumerate(final_df.columns.values):
                        worksheet.write(0, col_num, value, header_fmt)

                    # --- ประยุกต์ใช้ Format และกำหนดความกว้างคอลัมน์ ---
                    worksheet.set_column('A:A', 15, date_fmt)       # บังคับคอลัมน์ A เป็น m/d/yyyy
                    worksheet.set_column('B:D', 10, text_fmt)
                    worksheet.set_column(num_cols_range, 20, num_fmt)
                    worksheet.set_column('G:G', 80, text_fmt)

                output.seek(0)
                st.download_button(
                    label=f"📥 ดาวน์โหลด Excel ({bank_option})",
                    data=output,
                    file_name=f"Statement_{bank_option.split(' ')[0]}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

        except PasswordError:
            st.error("❌ รหัสผ่านไม่ถูกต้อง")
        except Exception as e:
            st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")

 else:
        st.sidebar.warning("⚠️ กรุณาเลือกไฟล์ PDF อย่างน้อย 1 ไฟล์")
