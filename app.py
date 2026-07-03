import io
import re
import pandas as pd
import pikepdf
import pdfplumber
import streamlit as st
from pikepdf import PasswordError

# ตั้งค่าหน้าเว็บ Streamlit
st.set_page_config(page_title="PDF Statement Converter", layout="wide")

# ================= 1. ฟังก์ชันช่วยเหลือ (Common Helpers) =================
def str_to_float(val_str):
    if val_str in [None, "", "-", " "]: return 0.0
    try:
        # ลบ comma และช่องว่าง
        clean_val = re.sub(r'[^\d.-]', '', str(val_str))
        return float(clean_val)
    except:
        return 0.0

# ================= 2. Logic สำหรับ KBank =================
def split_channel_and_detail_kbank(text):
    channels = [
        "EDC/K SHOP/MYQR", "โอนเข้า/หักบัญชีอัตโนมัติ", "K PLUS", "ตู้เติมเงิน / โมบาย แอปพลิ", 
        "Internet/Mobile KK", "K BIZ", "EDC", "โอนเข้าหักบัญชีอัตโนมัติ", "ATM", "CDM", 
        "BRANCH", "K-Cash Connect Plus", "Internet/Mobile GSB", "Internet/Mobile SCB", 
        "Internet/Mobile KTB", "Internet/Mobile TTB", "Internet/Mobile BAY"
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
                
                # Check for Date: DD-MM-YY หรือ DD/MM/YY
                date_match = re.match(r'^(\d{2}[-/]\d{2}[-/]\d{2})', line)
                if date_match:
                    date = date_match.group(1)
                    time_match = re.search(r'(\d{2}:\d{2})', line)
                    time = time_match.group(1) if time_match else ""
                    
                    # ค้นหาตัวเลขจำนวนเงินทั้งหมดในบรรทัด (เช่น ถอน/ฝาก และ ยอดคงเหลือ)
                    amounts = re.findall(r'[\d,]+\.\d{2}', line)
                    
                    # ตัด Date/Time ออกเพื่อหา Description
                    temp_text = line.replace(date, "", 1).strip()
                    if time: temp_text = temp_text.replace(time, "", 1).strip()
                    
                    # พยายามแยกคำอธิบายรายการ (Description)
                    desc = temp_text
                    if amounts:
                        desc = temp_text.split(amounts[0])[0].strip()
                    
                    amount_val, balance = 0.0, 0.0
                    if len(amounts) == 1:
                        balance = str_to_float(amounts[0])
                    elif len(amounts) >= 2:
                        # KBank มักจะวาง ถอน หรือ ฝาก ไว้ก่อน Balance
                        # ใช้ Keyword ช่วยเช็คว่าเป็นเงินเข้าหรือไม่
                        is_deposit = any(kw in desc for kw in ["รับเงิน", "คืนเงิน", "ฝาก", "เงินคืน", "Thai QR", "รับโอนเงิน", "CR"])
                        val = str_to_float(amounts[0])
                        amount_val = val if is_deposit else -val
                        balance = str_to_float(amounts[-1])
                    
                    # ส่วนที่เหลือหลังจาก Balance มักเป็น Channel/Detail
                    remaining = ""
                    if amounts:
                        parts = line.split(amounts[-1])
                        if len(parts) > 1: remaining = parts[-1].strip()
                    
                    chan, det = split_channel_and_detail_kbank(remaining)
                    all_parsed_rows.append([date, time, desc, amount_val, balance, chan, det])
                
                elif is_in_table and all_parsed_rows:
                    # กรณีเป็นบรรทัดต่อจากรายการหลัก (ไม่มีวันที่)
                    if any(x in line for x in ["หน้า", "แผ่นที่", "ยอดคงเหลือ"]): continue
                    c_extra, d_extra = split_channel_and_detail_kbank(line)
                    # นำไปต่อท้ายรายละเอียดของแถวล่าสุด
                    if c_extra != "-":
                        all_parsed_rows[-1][5] = (str(all_parsed_rows[-1][5]) + " " + c_extra).strip()
                    all_parsed_rows[-1][6] = (str(all_parsed_rows[-1][6]) + " " + d_extra).strip()
                    
    return all_parsed_rows

# ================= 3. Logic สำหรับ SCB =================
def parse_scb_pdf(pdf_stream):
    all_parsed_rows = []
    bf_keywords = ["ยอดยกมา", "BALANCE BROUGHT FORWARD"]
    ignore_keywords = ["Date/Time", "Code", "Channel", "Withdrawal", "Deposit", "Balance", "หน้าที่", "Page"]
    
    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text: continue
            lines = text.split('\n')
            for line in lines:
                line = line.strip()
                if not line or any(kw in line for kw in ignore_keywords): continue
                
                # Check for B/F
                if any(kw in line.upper() for kw in bf_keywords):
                    amounts = re.findall(r'[\d,]+\.\d{2}', line)
                    if amounts:
                        balance = str_to_float(amounts[-1])
                        all_parsed_rows.append([None, None, "B/F", "-", 0.0, balance, "ยอดยกมา"])
                    continue

                # Check Transaction: DD/MM/YYYY HH:MM
                transaction_match = re.match(r'^(\d{2}/\d{2}/\d{2,4})\s+(\d{2}:\d{2})', line)
                if transaction_match:
                    date_str = transaction_match.group(1)
                    time_str = transaction_match.group(2)
                    amounts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', line)
                    
                    # สกัด Code (คำแรกหลังจากเวลา)
                    line_content = line.replace(date_str, "").replace(time_str, "").strip()
                    parts = line_content.split()
                    code = parts[0] if len(parts) > 0 else "-"
                    
                    amount_val, balance_val = 0.0, 0.0
                    if len(amounts) >= 2:
                        balance_val = str_to_float(amounts[-1])
                        raw_amount = str_to_float(amounts[-2])
                        # Code เงินเข้าของ SCB
                        income_codes = ['X1', 'IN', 'IT', 'BT', 'DP', 'CR', 'C1', 'NR', 'SD']
                        amount_val = raw_amount if code.upper() in income_codes else -raw_amount
                    elif len(amounts) == 1:
                        balance_val = str_to_float(amounts[0])

                    # คำอธิบายที่เหลือ
                    desc = line_content.replace(code, "", 1)
                    for amt in amounts: desc = desc.replace(amt, "")
                    
                    all_parsed_rows.append([date_str, time_str, code, "-", amount_val, balance_val, desc.strip()])
                elif all_parsed_rows:
                    # ถ้าไม่ใช่บรรทัดใหม่ที่มีวันที่ ให้เอาข้อความไปต่อท้ายรายละเอียดแถวบน
                    all_parsed_rows[-1][6] = (all_parsed_rows[-1][6] + " " + line).strip()
                    
    return all_parsed_rows

# ================= 4. Streamlit UI & Excel Export =================

st.title("📑 PDF Statement to Excel (Clean Version)")
st.markdown("รองรับ KBank และ SCB (แบบไม่มีเส้นขอบตาราง)")

with st.sidebar:
    st.header("การตั้งค่า")
    bank_option = st.selectbox("เลือกธนาคาร", ["กสิกรไทย (KBank)", "ไทยพาณิชย์ (SCB)"])
    pdf_files = st.file_uploader("เลือกไฟล์ PDF", type="pdf", accept_multiple_files=True)
    password = st.text_input("รหัสผ่านไฟล์ (ถ้ามี)", type="password")
    convert_button = st.button("🚀 เริ่มแปลงไฟล์")

if convert_button:
    if not pdf_files:
        st.error("⚠️ กรุณาเลือกไฟล์ PDF")
    else:
        all_dfs = []
        progress_bar = st.progress(0)
        
        try:
            for idx, uploaded_file in enumerate(pdf_files):
                pdf_bytes = uploaded_file.read()
                try:
                    with pikepdf.open(io.BytesIO(pdf_bytes), password=password) as pdf:
                        unlocked_io = io.BytesIO()
                        pdf.save(unlocked_io)
                        unlocked_io.seek(0)
                        
                        if bank_option == "กสิกรไทย (KBank)":
                            rows = parse_kbank_pdf(unlocked_io)
                            cols = ["วันที่", "เวลา", "รายการ", "ยอดเงิน (ฝาก/ถอน)", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"]
                            df = pd.DataFrame(rows, columns=cols)
                            df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True, errors='coerce')
                        else:
                            rows = parse_scb_pdf(unlocked_io)
                            cols = ["วันที่", "เวลา", "Code", "ช่องทาง", "ยอดเงิน (ฝาก/ถอน)", "ยอดคงเหลือ", "รายละเอียด"]
                            df = pd.DataFrame(rows, columns=cols)
                            df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True, errors='coerce')
                        
                        all_dfs.append(df)
                except PasswordError:
                    st.error(f"❌ ไฟล์ {uploaded_file.name} รหัสผ่านไม่ถูกต้อง")
                
                progress_bar.progress((idx + 1) / len(pdf_files))

            if all_dfs:
                final_df = pd.concat(all_dfs, ignore_index=True)
                # ลบแถวที่ไม่มีข้อมูลสำคัญ
                final_df = final_df.dropna(subset=['ยอดคงเหลือ'])
                
                st.subheader("ตัวอย่างข้อมูลที่สกัดได้")
                st.dataframe(final_df, use_container_width=True)

                # --- จัดการ Excel Formatting ---
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                    sheet_name = 'Statement'
                    final_df.to_excel(writer, index=False, sheet_name=sheet_name)
                    workbook = writer.book
                    worksheet = writer.sheets[sheet_name]

                    # กำหนดสี
                    is_scb = (bank_option == "ไทยพาณิชย์ (SCB)")
                    header_color = '#4E2E7F' if is_scb else '#00A950'
                    # ช่วงคอลัมน์ที่เป็นตัวเลข (KBank: D-E, SCB: E-F)
                    num_cols = 'E:F' if is_scb else 'D:E'

                    # สร้าง Formats (border: 0 คือไม่มีเส้นขอบ)
                    header_fmt = workbook.add_format({
                        'bold': True, 'bg_color': header_color, 'font_color': 'white', 
                        'border': 0, 'align': 'center', 'valign': 'vcenter'
                    })
                    num_fmt = workbook.add_format({
                        'num_format': '#,##0.00', 'border': 0, 'align': 'right', 'valign': 'vcenter'
                    })
                    date_fmt = workbook.add_format({
                        'num_format': 'dd/mm/yyyy', 'border': 0, 'align': 'left'
                    })
                    text_fmt = workbook.add_format({'border': 0, 'valign': 'vcenter'})

                    # เขียน Header
                    for col_num, value in enumerate(final_df.columns.values):
                        worksheet.write(0, col_num, value, header_fmt)

                    # ตั้งค่าความกว้างคอลัมน์และ Format
                    worksheet.set_column('A:A', 12, date_fmt)
                    worksheet.set_column('B:D', 10, text_fmt)
                    worksheet.set_column(num_cols, 15, num_fmt)
                    worksheet.set_column('G:G', 60, text_fmt)

                output.seek(0)
                st.download_button(
                    label=f"📥 ดาวน์โหลด Excel ({bank_option})",
                    data=output,
                    file_name=f"Converted_Statement.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
            else:
                st.warning("ไม่พบข้อมูลที่สามารถแปลงได้")

        except Exception as e:
            st.error(f"❌ เกิดข้อผิดพลาดร้ายแรง: {str(e)}")
            st.info("คำแนะนำ: ตรวจสอบว่าไฟล์ PDF เป็นไฟล์ Statement ต้นฉบับจากธนาคาร")
