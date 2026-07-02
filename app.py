import io
import re
import pandas as pd
import pikepdf
import pdfplumber
import streamlit as st
from pikepdf import PasswordError

# ตั้งค่าหน้าเว็บ Streamlit
st.set_page_config(page_title="PDF Statement Converter", layout="wide")

# CSS สำหรับตกแต่งเพิ่มเติม (ถ้าต้องการปรับแต่ง UI ให้ดูเนียนขึ้น)
st.markdown("""
    <style>
    .stButton>button {
        width: 100%;
        border-radius: 5px;
        height: 3em;
        background-color: #343a40;
        color: white;
    }
    .main {
        background-color: #1e1e26;
    }
    </style>
    """, unsafe_allow_html=True)

# ================= 1. ฟังก์ชันช่วยเหลือ (Utility) =================
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

# ================= 2. Logic การอ่าน PDF =================
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
                if any(kw in line for kw in table_headers):
                    is_in_table = True
                    continue
                if not is_in_table or any(kw in line for kw in ["Total", "รวมทั้งสิ้น", "จบรายการ"]):
                    is_in_table = False
                    continue

                date_match = re.match(r'^(\d{2}-\d{2}-\d{2})', line)
                if date_match:
                    if current_row: all_parsed_rows.append(current_row)
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
                    current_row = [date, time, desc, amount_val, balance, chan, det]
                elif is_in_table:
                    if any(x in line for x in ["หน้า", "แผ่นที่", "ยอดคงเหลือ"]): continue
                    c_extra, d_extra = split_channel_and_detail(line)
                    all_parsed_rows.append(["", "", "", None, None, c_extra if c_extra != "-" else "", d_extra])

        if current_row: all_parsed_rows.append(current_row)

    final_rows = []
    bf_occurrence = 0
    empty_row_buffer = []

    def flush_buffer(buffer_list, target_list):
        if len(buffer_list) == 1:
            target_list.append(buffer_list[0])

    for row in all_parsed_rows:
        desc = str(row[2])
        amount = row[3]
        is_bf = any(kw in desc for kw in bf_keywords)

        if is_bf:
            flush_buffer(empty_row_buffer, final_rows)
            empty_row_buffer = []
            bf_occurrence += 1
            if bf_occurrence <= 1:
                final_rows.append(row)
            continue

        if amount is not None:
            flush_buffer(empty_row_buffer, final_rows)
            empty_row_buffer = []
            final_rows.append(row)
        else:
            if row[5] != "-" or row[6] != "":
                empty_row_buffer.append(row)

    flush_buffer(empty_row_buffer, final_rows)
    return final_rows

# ================= 3. ส่วนการแสดงผล (Streamlit UI) =================

# ใช้ columns เพื่อจัดหน้าจอให้อยู่กึ่งกลาง
main_col1, main_col2, main_col3 = st.columns([1, 2, 1])

with main_col2:
    st.title("📑 PDF to Excel Converter")
    st.write("แปลงไฟล์ Statement ธนาคารให้เป็น Excel ได้ง่ายๆ")
    st.divider()

    # --- เพิ่มตัวเลือกธนาคาร ---
    bank_option = st.selectbox(
        "เลือกรูปแบบธนาคาร",
        ("กสิกรไทย (KBank)", "ไทยพาณิชย์ (SCB)", "กรุงเทพ (BBL)", "กรุงไทย (KTB)", "อื่น ๆ")
    )

    # --- ส่วนอัปโหลดไฟล์ ---
    st.write("**เลือกไฟล์ PDF**")
    pdf_file = st.file_uploader("ลากไฟล์มาวางที่นี่", type="pdf", label_visibility="collapsed")
    st.caption("จำกัดขนาด 200MB ต่อไฟล์ • PDF เท่านั้น")

    # --- ส่วนรหัสผ่าน ---
    st.write("**รหัสผ่านไฟล์ PDF (ถ้ามี)**")
    password = st.text_input("ใส่รหัสผ่านที่นี่", type="password", label_visibility="collapsed")

    # --- ปุ่มเริ่มแปลงไฟล์ ---
    st.write("") # เว้นวรรค
    convert_button = st.button("เริ่มการแปลงไฟล์")

    # logic การทำงานเมื่อกดปุ่ม
    if convert_button:
        if pdf_file:
            try:
                with st.spinner("กำลังประมวลผล..."):
                    # 1. ปลดล็อก PDF
                    pdf_bytes = pdf_file.read()
                    # ใช้ pikepdf ปลดล็อครหัสผ่าน
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
                        
                        # 5. แสดงผลและปุ่มดาวน์โหลด
                        st.success(f"✅ แปลงไฟล์สำหรับธนาคาร {bank_option} สำเร็จ!")
                        st.dataframe(df.head(10), use_container_width=True)
                        
                        st.download_button(
                            label="📥 ดาวน์โหลดไฟล์ Excel",
                            data=output,
                            file_name=f"{pdf_file.name.split('.')[0]}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True
                        )

            except PasswordError:
                st.error("❌ รหัสผ่านไม่ถูกต้อง หรือไฟล์ถูกล็อกด้วยระบบที่ซับซ้อน")
            except Exception as e:
                st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
        else:
            st.warning("⚠️ กรุณาเลือกไฟล์ PDF ก่อนกดปุ่ม")
