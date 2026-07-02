import io
import re
import pandas as pd
import pikepdf
import pdfplumber
import streamlit as st
from pikepdf import PasswordError

# ตั้งค่าหน้าเว็บ Streamlit
st.set_page_config(page_title="PDF Statement Converter", layout="wide")

# ================= 1. ฟังก์ชันช่วยเหลือ =================
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

def str_to_float(val_str):
    if val_str in [None, "", "-", " "]: return None
    try: return float(str(val_str).replace(',', ''))
    except: return None

# ================= 2. Logic การอ่าน PDF (แก้ไขลำดับการเก็บแถว) =================
def parse_pdf_content(pdf_stream):
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
                
                # ตรวจสอบหัวตาราง
                if any(kw in line for kw in table_headers):
                    is_in_table = True
                    continue
                
                # ตรวจสอบจุดสิ้นสุดตาราง
                if not is_in_table or any(kw in line for kw in ["Total", "รวมทั้งสิ้น", "จบรายการ"]):
                    is_in_table = False
                    continue

                # ตรวจสอบบรรทัดใหม่ (ที่มีวันที่)
                date_match = re.match(r'^(\d{2}-\d{2}-\d{2})', line)
                
                if date_match:
                    date = date_match.group(1)
                    time_match = re.search(r'(\d{2}:\d{2})', line)
                    time = time_match.group(1) if time_match else ""
                    
                    # หายอดเงินและยอดคงเหลือ (ตัวเลขที่มีทศนิยม)
                    amounts = re.findall(r'[\d,]+\.\d{2}', line)
                    
                    # แยกคำบรรยาย (รายการ)
                    temp_text = line.replace(date, "", 1).strip()
                    if time: temp_text = temp_text.replace(time, "", 1).strip()
                    
                    # ค้นหาข้อความรายการก่อนถึงตัวเลขตัวแรก
                    desc = temp_text.split(amounts[0])[0].strip() if amounts else temp_text
                    
                    amount_val, balance = None, None
                    if len(amounts) == 1:
                        balance = str_to_float(amounts[0])
                    elif len(amounts) >= 2:
                        # แยกฝาก/ถอน
                        is_deposit = any(kw in desc for kw in ["รับเงิน", "คืนเงิน", "ฝาก", "เงินคืน", "Thai QR", "รับโอนเงิน"])
                        val = str_to_float(amounts[0])
                        amount_val = val if is_deposit else -val
                        balance = str_to_float(amounts[-1])

                    # หาช่องทางและรายละเอียดที่เหลือในบรรทัดเดียวกัน
                    remaining = ""
                    if amounts:
                        parts = line.split(amounts[-1])
                        if len(parts) > 1: remaining = parts[-1].strip()
                    
                    chan, det = split_channel_and_detail(remaining)
                    
                    # เก็บ "บรรทัดหลัก" ลงไปทันทีเพื่อให้ลำดับถูกต้อง
                    all_parsed_rows.append([date, time, desc, amount_val, balance, chan, det])

                elif is_in_table:
                    # กรณีเป็น "บรรทัดต่อขยาย" (ไม่มีวันที่)
                    if any(x in line for x in ["หน้า", "แผ่นที่", "ยอดคงเหลือ"]): continue
                    if any(kw in line for kw in bf_keywords): continue
                    
                    c_extra, d_extra = split_channel_and_detail(line)
                    # เก็บเป็นแถวว่าง แต่มีรายละเอียด (จะต่อท้ายบรรทัดหลักเสมอ)
                    all_parsed_rows.append(["", "", "", None, None, c_extra if c_extra != "-" else "", d_extra])

    # กรองเอา "ยอดยกมา" ออกให้เหลือแค่อันแรก (ถ้าต้องการ) หรือส่งออกทั้งหมด
    return all_parsed_rows

# ================= 3. ส่วนการแสดงผล =================

st.title("📑 PDF Statement to Excel")
st.info("อัปโหลดไฟล์เพื่อแปลงข้อมูล (รองรับการดึงรายละเอียดแบบหลายบรรทัด)")

with st.sidebar:
    st.header("ตั้งค่าการแปลงไฟล์")
    bank_option = st.selectbox("เลือกรูปแบบธนาคาร", ("กสิกรไทย (KBank)", "อื่น ๆ"))
    st.divider()
    st.write("**เลือกไฟล์ PDF**")
    pdf_file = st.file_uploader("Upload", type="pdf", label_visibility="collapsed")
    st.write("**รหัสผ่านไฟล์ PDF (ถ้ามี)**")
    password = st.text_input("Password", type="password", label_visibility="collapsed")
    st.write("")
    convert_button = st.button("เริ่มการแปลงไฟล์")

if convert_button:
    if pdf_file:
        try:
            with st.spinner("กำลังประมวลผล..."):
                pdf_bytes = pdf_file.read()
                with pikepdf.open(io.BytesIO(pdf_bytes), password=password) as pdf:
                    unlocked_io = io.BytesIO()
                    pdf.save(unlocked_io)
                    unlocked_io.seek(0)
                    
                    data_rows = parse_pdf_content(unlocked_io)
                    header = ["วันที่", "เวลา", "รายการ", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"]
                    df = pd.DataFrame(data_rows, columns=header)
                    
                    # แปลงวันที่ (เฉพาะแถวที่มีข้อมูล)
                    df['วันที่'] = pd.to_datetime(df['วันที่'], format='%d-%m-%y', errors='coerce')

                    output = io.BytesIO()
                    with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='dd/mm/yyyy') as writer:
                        df.to_excel(writer, index=False, sheet_name='Statement')
                        workbook, worksheet = writer.book, writer.sheets['Statement']
                        num_fmt = workbook.add_format({'num_format': '#,##0.00'})
                        worksheet.set_column('A:A', 15)
                        worksheet.set_column('D:E', 15, num_fmt)
                        worksheet.set_column('G:G', 50)
                    
                    output.seek(0)
                    st.success(f"✅ แปลงไฟล์สำเร็จ")
                    st.dataframe(df, use_container_width=True)
                    st.download_button(label="📥 ดาวน์โหลดไฟล์ Excel", data=output, file_name=f"Converted_Statement.xlsx")
        except PasswordError:
            st.sidebar.error("❌ รหัสผ่านไม่ถูกต้อง")
        except Exception as e:
            st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
    else:
        st.sidebar.warning("⚠️ กรุณาเลือกไฟล์ PDF")
