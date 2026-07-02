import io
import re
import pandas as pd
import pikepdf
import pdfplumber
import streamlit as st
from pikepdf import PasswordError

# ตั้งค่าหน้าเว็บ Streamlit
st.set_page_config(page_title="PDF Statement Converter", layout="wide")

# ================= 1. ฟังก์ชันช่วยเหลือ (คงเดิม) =================
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

# ================= 2. Logic การอ่าน PDF (คงเดิม) =================
def parse_pdf_content(pdf_stream):
    all_parsed_rows = []
    bf_keywords = ["ยอดยกมา", "Balance Brought Forward", "Brought Forward"]
    table_headers = ["เวลา/", "วันที่มีผล", "รายการ", "ถอนเงิน", "ฝากเงิน", "ยอดคงเหลือ"]
    current_row = None

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
                
                # ตรวจสอบจุดจบตาราง
                if is_in_table and any(kw in line for kw in ["Total", "รวมทั้งสิ้น", "จบรายการ"]):
                    is_in_table = False
                    continue

                if not is_in_table:
                    continue

                # ตรวจสอบว่าเป็นบรรทัดใหม่ที่มี "วันที่" หรือไม่
                date_match = re.match(r'^(\d{2}-\d{2}-\d{2})', line)
                
                if date_match:
                    # ถ้าเจอวันที่ใหม่ ให้เก็บแถวเก่าลง list ก่อน
                    if current_row:
                        all_parsed_rows.append(current_row)
                    
                    date = date_match.group(1)
                    time_match = re.search(r'(\d{2}:\d{2})', line)
                    time = time_match.group(1) if time_match else ""
                    
                    # ค้นหาตัวเลขยอดเงิน (ถอน/ฝาก/คงเหลือ)
                    amounts = re.findall(r'[\d,]+\.\d{2}', line)
                    
                    # แยกคำอธิบายรายการ (Description)
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

                    # หาข้อมูลที่เหลือหลังยอดเงินตัวสุดท้าย (Channel/Detail)
                    remaining = ""
                    if amounts:
                        parts = line.split(amounts[-1])
                        if len(parts) > 1: remaining = parts[-1].strip()
                    
                    chan, det = split_channel_and_detail(remaining)
                    
                    # สร้างแถวใหม่
                    current_row = [date, time, desc, amount_val, balance, chan, det]
                
                else:
                    # --- จุดที่แก้ไข: ถ้าไม่เจอวันที่ แต่ยังอยู่ในตาราง ให้เอาข้อความไป "ต่อท้าย" รายละเอียดของแถวปัจจุบัน ---
                    if current_row:
                        # กรองคำที่ไม่ต้องการออก
                        if any(x in line for x in ["หน้า", "แผ่นที่", "ยอดคงเหลือ", "วันที่"]): 
                            continue
                        
                        # แยกส่วน Channel/Detail จากบรรทัดเสริม (ถ้ามี)
                        c_extra, d_extra = split_channel_and_detail(line)
                        
                        # ถ้ามี Channel ใหม่ให้ใส่ (ถ้าว่างให้ข้าม)
                        if c_extra != "-" and current_row[5] == "-":
                            current_row[5] = c_extra
                        
                        # ต่อท้ายรายละเอียด (ใช้ช่องว่างคั่น)
                        if d_extra:
                            current_row[6] = f"{current_row[6]} {d_extra}".strip()

        # เก็บแถวสุดท้ายหลังจบลูป
        if current_row:
            all_parsed_rows.append(current_row)

    # กรองเฉพาะรายการที่เป็นธุรกรรมจริงๆ (ตัด 'ยอดยกมา' ออกถ้าต้องการ หรือเก็บไว้ก็ได้)
    final_rows = []
    for row in all_parsed_rows:
        desc = str(row[2])
        if not any(kw in desc for kw in bf_keywords):
            final_rows.append(row)
            
    return final_rows

# ================= 3. ส่วนการแสดงผล (Sidebar UI) =================

st.title("📑 PDF Statement to Excel")
st.info("อัปโหลดไฟล์ที่แถบด้านข้าง เพื่อเริ่มต้นการแปลงข้อมูล")

with st.sidebar:
    st.header("ตั้งค่าการแปลงไฟล์")
    
    # 1. เพิ่มตัวเลือกธนาคาร
    bank_option = st.selectbox(
        "เลือกรูปแบบธนาคาร",
        ("กสิกรไทย (KBank)", "ไทยพาณิชย์ (SCB)", "กรุงไทย (KTB)", "กรุงเทพ (BBL)", "อื่น ๆ")
    )
    
    st.divider()

    # 2. เลือกไฟล์ PDF (ตามรูปแบบในรูปภาพ)
    st.write("**เลือกไฟล์ PDF**")
    pdf_file = st.file_uploader("Upload", type="pdf", label_visibility="collapsed")
    st.caption("200MB per file • PDF")

    # 3. รหัสผ่านไฟล์ PDF (ตามรูปแบบในรูปภาพ)
    st.write("**รหัสผ่านไฟล์ PDF (ถ้ามี)**")
    password = st.text_input("Password", type="password", label_visibility="collapsed")

    # 4. ปุ่มเริ่มการแปลงไฟล์
    st.write("")
    convert_button = st.button("เริ่มการแปลงไฟล์")

# --- Logic การทำงานเมื่อกดปุ่ม ---
if convert_button:
    if pdf_file:
        try:
            with st.spinner("กำลังประมวลผล..."):
                # 1. ปลดล็อก PDF
                pdf_bytes = pdf_file.read()
                with pikepdf.open(io.BytesIO(pdf_bytes), password=password) as pdf:
                    unlocked_io = io.BytesIO()
                    pdf.save(unlocked_io)
                    unlocked_io.seek(0)
                    
                    # 2. อ่านข้อมูล
                    data_rows = parse_pdf_content(unlocked_io)
                    header = ["วันที่", "เวลา", "รายการ", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"]
                    df = pd.DataFrame(data_rows, columns=header)
                    
                    # 3. จัดรูปแบบข้อมูล
                    df['วันที่'] = pd.to_datetime(df['วันที่'], format='%d-%m-%y', errors='coerce')

                    # 4. สร้าง Excel
                    output = io.BytesIO()
                    with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='mm/dd/yyyy') as writer:
                        df.to_excel(writer, index=False, sheet_name='Statement')
                        workbook, worksheet = writer.book, writer.sheets['Statement']
                        
                        date_fmt = workbook.add_format({'num_format': 'mm/dd/yyyy', 'align': 'left'})
                        num_fmt = workbook.add_format({'num_format': '_(* #,##0.00_);_(* (#,##0.00);_(* "-"??_);_(@_)'})

                        worksheet.set_column('A:A', 12, date_fmt)
                        worksheet.set_column('D:E', 18, num_fmt)
                        worksheet.set_column('B:B', 10)
                        worksheet.set_column('C:C', 25)
                        worksheet.set_column('F:G', 45)
                    
                    output.seek(0)
                    
                    # 5. แสดงผลในหน้าจอหลัก
                    st.success(f"✅ แปลงไฟล์สำเร็จ ({bank_option})")
                    st.dataframe(df, use_container_width=True)
                    
                    st.download_button(
                        label="📥 ดาวน์โหลดไฟล์ Excel",
                        data=output,
                        file_name=f"{pdf_file.name.split('.')[0]}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )

        except PasswordError:
            st.sidebar.error("❌ รหัสผ่านไม่ถูกต้อง")
        except Exception as e:
            st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
    else:
        st.sidebar.warning("⚠️ กรุณาเลือกไฟล์ PDF")
