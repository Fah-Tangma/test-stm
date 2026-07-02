import io
import re
import pandas as pd
import pikepdf
import pdfplumber
import streamlit as st
from pikepdf import PasswordError

# ตั้งค่าหน้าเว็บ Streamlit
st.set_page_config(page_title="PDF Statement Converter (KBank & KTB)", layout="wide")

# ================= 1. ฟังก์ชันช่วยเหลือทั่วไป =================
def str_to_float(val_str):
    if val_str in [None, "", "-", " "]: return None
    try: 
        # ลบ comma และช่องว่างออก
        clean_val = str(val_str).replace(',', '').strip()
        return float(clean_val)
    except: 
        return None

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

# ================= 2. Logic สำหรับกสิกรไทย (KBank - โค้ดเดิม) =================
def parse_kbank_content(pdf_stream):
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

    # กรองเอาเฉพาะรายการที่สมบูรณ์ (เหมือน Logic เดิม)
    temp_list_bf = []
    found_first_bf = False
    for row in all_parsed_rows:
        is_bf_row = any(kw in str(row[2]) for kw in bf_keywords)
        if is_bf_row:
            if not found_first_bf:
                temp_list_bf.append(row)
                found_first_bf = True
        else:
            temp_list_bf.append(row)

    final_filtered_rows = []
    i, n = 0, len(temp_list_bf)
    while i < n:
        if temp_list_bf[i][3] is not None:
            final_filtered_rows.append(temp_list_bf[i])
            i += 1
        else:
            empty_block = []
            while i < n and temp_list_bf[i][3] is None:
                if any(kw in str(temp_list_bf[i][2]) for kw in bf_keywords):
                    final_filtered_rows.append(temp_list_bf[i])
                    i += 1
                    continue
                empty_block.append(temp_list_bf[i])
                i += 1
            if len(empty_block) == 1:
                final_filtered_rows.append(empty_block[0])
    return final_filtered_rows

# ================= 3. Logic สำหรับกรุงไทย (KTB - โค้ดเพิ่มเติม) =================
def parse_ktb_content(pdf_stream):
    all_parsed_rows = []
    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            # ใช้ extract_table เนื่องจาก Layout กรุงไทยเป็นตารางชัดเจน
            table = page.extract_table({
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
            })
            
            if not table: continue
            
            for row in table:
                # ตรวจสอบว่าเป็นแถวรายการ (คอลัมน์แรกมีวันที่รูปแบบ DD/MM/YY)
                if row[0] and re.search(r'\d{2}/\d{2}/\d{2}', row[0]):
                    # แยกวันที่และเวลา (KTB มักขึ้นบรรทัดใหม่ใน Cell เดียวกัน)
                    dt_parts = row[0].split('\n')
                    date = dt_parts[0].strip()
                    time = dt_parts[1].strip() if len(dt_parts) > 1 else ""
                    
                    desc = row[1].replace('\n', ' ').strip() if row[1] else ""
                    detail = row[2].replace('\n', ' ').strip() if row[2] else ""
                    
                    withdraw = str_to_float(row[3])
                    deposit = str_to_float(row[4])
                    balance = str_to_float(row[5])
                    branch = row[6].strip() if row[6] else ""
                    
                    # รวมยอดถอน/ฝากเป็นคอลัมน์เดียวเหมือน KBank
                    # ถอนเป็นลบ ฝากเป็นบวก
                    amount_val = deposit if deposit else (-withdraw if withdraw else 0)
                    
                    all_parsed_rows.append([date, time, desc, amount_val, balance, f"Branch: {branch}", detail])
                    
    return all_parsed_rows

# ================= 4. ส่วน UI และการประมวลผลหลัก =================

st.title("📑 Bank Statement to Excel Converter")

with st.sidebar:
    st.header("ตั้งค่าการแปลงไฟล์")
    bank_option = st.selectbox("เลือกรูปแบบธนาคาร", ["กสิกรไทย (KBank)", "กรุงไทย (KTB)"])
    st.divider()
    pdf_files = st.file_uploader("เลือกไฟล์ PDF (สูงสุด 5 ไฟล์)", type="pdf", accept_multiple_files=True)
    password = st.text_input("รหัสผ่านไฟล์ PDF (ถ้ามี)", type="password")
    convert_button = st.button("เริ่มการแปลงไฟล์ทั้งหมด")

if convert_button:
    if pdf_files:
        if len(pdf_files) > 5:
            st.error("❌ กรุณาเลือกไฟล์ไม่เกิน 5 ไฟล์")
        else:
            all_dataframes = []
            try:
                progress_bar = st.progress(0)
                for index, uploaded_file in enumerate(pdf_files):
                    st.write(f"⏳ กำลังอ่านไฟล์: {uploaded_file.name}")
                    pdf_bytes = uploaded_file.read()
                    
                    with pikepdf.open(io.BytesIO(pdf_bytes), password=password) as pdf:
                        unlocked_io = io.BytesIO()
                        pdf.save(unlocked_io)
                        unlocked_io.seek(0)
                        
                        # เลือกฟังก์ชันตามธนาคารที่เลือก
                        if bank_option == "กสิกรไทย (KBank)":
                            data_rows = parse_kbank_content(unlocked_io)
                            date_format = '%d-%m-%y'
                        else:
                            data_rows = parse_ktb_content(unlocked_io)
                            date_format = '%d/%m/%y'
                        
                        header = ["วันที่", "เวลา", "รายการ", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "ช่องทาง/สาขา", "รายละเอียด"]
                        df_single = pd.DataFrame(data_rows, columns=header)
                        
                        # พยายามแปลงวันที่ให้เป็น datetime เพื่อการจัดรูปแบบใน Excel
                        df_single['วันที่'] = pd.to_datetime(df_single['วันที่'], format=date_format, errors='coerce').dt.date
                        
                        all_dataframes.append(df_single)
                    
                    progress_bar.progress((index + 1) / len(pdf_files))

                if all_dataframes:
                    final_df = pd.concat(all_dataframes, ignore_index=True)
                    
                    # สร้างไฟล์ Excel
                    output = io.BytesIO()
                    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                        final_df.to_excel(writer, index=False, sheet_name='Combined_Statement')
                        workbook = writer.book
                        worksheet = writer.sheets['Combined_Statement']
                        
                        # Format ตัวเลขและวันที่
                        num_fmt = workbook.add_format({'num_format': '#,##0.00', 'align': 'right'})
                        
                        worksheet.set_column('A:A', 12) # วันที่
                        worksheet.set_column('B:B', 10) # เวลา
                        worksheet.set_column('C:C', 20) # รายการ
                        worksheet.set_column('D:E', 18, num_fmt) # จำนวนเงิน/ยอดคงเหลือ
                        worksheet.set_column('F:F', 20) # ช่องทาง
                        worksheet.set_column('G:G', 50) # รายละเอียด
                    
                    output.seek(0)
                    st.success(f"✅ รวมไฟล์สำเร็จ ({len(pdf_files)} ไฟล์)")
                    st.dataframe(final_df, use_container_width=True)
                    st.download_button(
                        label="📥 ดาวน์โหลดไฟล์ Excel รวม", 
                        data=output, 
                        file_name=f"Combined_Statement_{bank_option.split(' ')[0]}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
            except PasswordError:
                st.error("❌ รหัสผ่านไม่ถูกต้อง")
            except Exception as e:
                st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
    else:
        st.sidebar.warning("⚠️ กรุณาเลือกไฟล์ PDF อย่างน้อย 1 ไฟล์")
