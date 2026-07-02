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

# ================= 2. Logic การอ่าน PDF พร้อมเงื่อนไขการลบแถว =================
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

                elif is_in_table:
                    if any(x in line for x in ["หน้า", "แผ่นที่", "ยอดคงเหลือ"]): continue
                    c_extra, d_extra = split_channel_and_detail(line)
                    all_parsed_rows.append(["", "", "", None, None, c_extra if c_extra != "-" else "", d_extra])

    # --- เริ่มกระบวนการกรองข้อมูลตามเงื่อนไข ---
    
    # เงื่อนไขที่ 2: ลบ "ยอดยกมา" ทั้งหมด ยกเว้นอันแรกสุดที่เจอ
    temp_list_bf = []
    found_first_bf = False
    for row in all_parsed_rows:
        is_bf_row = any(kw in str(row[2]) for kw in bf_keywords)
        if is_bf_row:
            if not found_first_bf:
                temp_list_bf.append(row)
                found_first_bf = True
            # ถ้าเจอตัวที่สองเป็นต้นไป จะไม่ถูก append เข้าไป (ลบทิ้ง)
        else:
            temp_list_bf.append(row)

    # เงื่อนไขที่ 1: ลบกลุ่มแถวว่าง (ไม่มีจำนวนเงิน) ที่ติดต่อกันมากกว่า 1 แถว
    final_filtered_rows = []
    i = 0
    n = len(temp_list_bf)
    while i < n:
        # ถ้าแถวนี้มีจำนวนเงิน (amount_val อยู่ index 3) ให้เก็บไว้ปกติ
        if temp_list_bf[i][3] is not None:
            final_filtered_rows.append(temp_list_bf[i])
            i += 1
        else:
            # เริ่มตรวจสอบกลุ่มแถวว่าง (รายละเอียดเสริม)
            empty_block = []
            while i < n and temp_list_bf[i][3] is None:
                # ตรวจสอบเพิ่มเติมว่าไม่ใช่แถว "ยอดยกมา" ตัวแรกที่เราเก็บไว้ (ซึ่งบางทีอาจไม่มี amount_val)
                if any(kw in str(temp_list_bf[i][2]) for kw in bf_keywords):
                    final_filtered_rows.append(temp_list_bf[i])
                    i += 1
                    continue
                
                empty_block.append(temp_list_bf[i])
                i += 1
            
            # ถ้ามีแถวว่างบรรทัดเดียว (เป็นรายละเอียดของรายการก่อนหน้า) ให้เก็บไว้
            if len(empty_block) == 1:
                final_filtered_rows.append(empty_block[0])
            # ถ้ามีมากกว่า 1 แถว (len > 1) บล็อกนี้จะถูกข้ามไปทั้งหมด (ลบออก)
            
    return final_filtered_rows

# ================= 3. ส่วนการแสดงผล =================

st.title("📑 PDF Statement to Excel")

with st.sidebar:
    st.header("ตั้งค่าการแปลงไฟล์")
    bank_option = st.selectbox("เลือกรูปแบบธนาคาร", ["กสิกรไทย (KBank)"])
    st.divider()
    pdf_file = st.file_uploader("เลือกไฟล์ PDF", type="pdf")
    password = st.text_input("รหัสผ่านไฟล์ PDF (ถ้ามี)", type="password")
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
                    
                    df['วันที่'] = pd.to_datetime(df['วันที่'], format='%d-%m-%y', errors='coerce')

                    output = io.BytesIO()
                    with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='m/d/yyyy') as writer:
                        df.to_excel(writer, index=False, sheet_name='Statement')
                        workbook = writer.book
                        worksheet = writer.sheets['Statement']
                        
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
                    st.success(f"✅ แปลงไฟล์สำเร็จ")
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
