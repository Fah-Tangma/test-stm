import io
import re
import pandas as pd
import pikepdf
import pdfplumber
import streamlit as st
from pikepdf import PasswordError

# ตั้งค่าหน้าเว็บ Streamlit
st.set_page_config(page_title="PDF Statement Converter", layout="wide")

# ปรับแต่ง UI
st.markdown("""
    <style>
    .stDownloadButton > button {
        width: 100%;
        background-color: #007bff;
        color: white;
    }
    </style>
    """, unsafe_allow_html=True)

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

# ================= 2. Logic การอ่าน PDF (แก้ไขเรื่องบรรทัดสลับ) =================
def parse_pdf_content(pdf_stream):
    all_parsed_rows = []
    bf_keywords = ["ยอดยกมา", "Balance Brought Forward", "Brought Forward"]
    table_headers = ["เวลา/", "วันที่มีผล", "รายการ", "ถอนเงิน", "ฝากเงิน", "ยอดคงเหลือ"]
    
    current_row = None  # ตัวแปรเก็บข้อมูลรายการปัจจุบัน

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
                
                # ตรวจสอบจุดจบรายการ
                if is_in_table and any(kw in line for kw in ["Total", "รวมทั้งสิ้น", "จบรายการ"]):
                    is_in_table = False
                    continue

                if not is_in_table: continue

                # ตรวจสอบว่าเป็นบรรทัดเริ่มต้นรายการใหม่ (มีวันที่) หรือไม่
                date_match = re.match(r'^(\d{2}-\d{2}-\d{2})', line)
                
                if date_match:
                    # บันทึกรายการก่อนหน้าที่สะสมไว้ลง list
                    if current_row:
                        all_parsed_rows.append(current_row)
                    
                    date = date_match.group(1)
                    time_match = re.search(r'(\d{2}:\d{2})', line)
                    time = time_match.group(1) if time_match else ""
                    amounts = re.findall(r'[\d,]+\.\d{2}', line)
                    
                    # แยก Description
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
                    
                    # เริ่มต้นสร้างรายการใหม่
                    current_row = [date, time, desc, amount_val, balance, chan, det]
                
                else:
                    # --- กรณีบรรทัดไม่มีวันที่ (ข้อมูลเสริม) ---
                    if current_row:
                        # ข้ามบรรทัดที่ไม่ใช่ข้อมูล
                        if any(x in line for x in ["หน้า", "แผ่นที่", "ยอดคงเหลือ"]): continue
                        
                        c_extra, d_extra = split_channel_and_detail(line)
                        
                        # รวม Channel ถ้าของเดิมไม่มี
                        if c_extra != "-" and current_row[5] == "-":
                            current_row[5] = c_extra
                        
                        # รวมรายละเอียด (ขึ้นบรรทัดใหม่ด้วย \n)
                        if d_extra:
                            if current_row[6] and current_row[6] != "-":
                                current_row[6] += f"\n{d_extra}"
                            else:
                                current_row[6] = d_extra

        # เก็บรายการสุดท้ายหลังจบลูป
        if current_row:
            all_parsed_rows.append(current_row)

    # กรอง 'ยอดยกมา' และจัดระเบียบครั้งสุดท้าย
    final_rows = []
    bf_count = 0
    for row in all_parsed_rows:
        desc = str(row[2])
        # ตรวจสอบว่าเป็น 'ยอดยกมา' หรือไม่
        if any(kw in desc for kw in bf_keywords):
            bf_count += 1
            if bf_count <= 1: # เก็บเฉพาะยอดยกมาอันแรกสุดของไฟล์
                final_rows.append(row)
            continue
        final_rows.append(row)

    return final_rows

# ================= 3. ส่วนการแสดงผล (UI) =================

st.title("📑 PDF Statement to Excel")
st.info("อัปโหลดไฟล์ที่แถบด้านข้าง เพื่อเริ่มต้นการแปลงข้อมูล")

with st.sidebar:
    st.header("ตั้งค่าการแปลงไฟล์")
    bank_option = st.selectbox("เลือกรูปแบบธนาคาร", ("กสิกรไทย (KBank)", "อื่น ๆ"))
    st.divider()
    st.write("**เลือกไฟล์ PDF**")
    pdf_file = st.file_uploader("Upload", type="pdf", label_visibility="collapsed")
    st.caption("200MB per file • PDF")
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
                    
                    # ประมวลผลข้อมูล
                    data_rows = parse_pdf_content(unlocked_io)
                    
                    header = ["วันที่", "เวลา", "รายการ", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"]
                    df = pd.DataFrame(data_rows, columns=header)
                    
                    # แปลงวันที่เพื่อใช้ใน Excel
                    df['วันที่'] = pd.to_datetime(df['วันที่'], format='%d-%m-%y', errors='coerce')

                    # สร้าง Excel
                    output = io.BytesIO()
                    with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='dd/mm/yyyy') as writer:
                        df.to_excel(writer, index=False, sheet_name='Statement')
                        workbook, worksheet = writer.book, writer.sheets['Statement']
                        
                        # Format สำหรับ Wrap Text (ขึ้นบรรทัดใหม่ใน Cell)
                        wrap_fmt = workbook.add_format({'text_wrap': True, 'valign': 'top', 'font_name': 'Tahoma', 'font_size': 10})
                        date_fmt = workbook.add_format({'num_format': 'dd/mm/yyyy', 'align': 'left', 'valign': 'top'})
                        num_fmt = workbook.add_format({'num_format': '#,##0.00', 'valign': 'top'})

                        # ตั้งค่าความกว้างคอลัมน์และเปิด Wrap Text
                        worksheet.set_column('A:A', 12, date_fmt)
                        worksheet.set_column('B:B', 8, wrap_fmt)
                        worksheet.set_column('C:C', 15, wrap_fmt)
                        worksheet.set_column('D:E', 15, num_fmt)
                        worksheet.set_column('F:F', 15, wrap_fmt)
                        worksheet.set_column('G:G', 50, wrap_fmt) # คอลัมน์รายละเอียดสำคัญที่สุดเรื่อง Wrap
                    
                    output.seek(0)
                    
                    st.success(f"✅ แปลงไฟล์สำเร็จ ({bank_option})")
                    # แสดงตัวอย่างข้อมูล (หมายเหตุ: ใน streamlit dataframe อาจไม่โชว์การขึ้นบรรทัดใหม่ชัดเจน แต่ใน Excel จะถูกต้องครับ)
                    st.dataframe(df, use_container_width=True)
                    
                    st.download_button(
                        label="📥 ดาวน์โหลดไฟล์ Excel",
                        data=output,
                        file_name=f"Converted_{pdf_file.name.split('.')[0]}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )

        except PasswordError:
            st.sidebar.error("❌ รหัสผ่านไม่ถูกต้อง")
        except Exception as e:
            st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
    else:
        st.sidebar.warning("⚠️ กรุณาเลือกไฟล์ PDF")
