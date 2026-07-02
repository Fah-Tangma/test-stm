import io
import re
import pandas as pd
import pikepdf
import pdfplumber
import streamlit as st
from pikepdf import PasswordError

# ตั้งค่าหน้าเว็บ Streamlit
st.set_page_config(page_title="PDF Statement Converter", page_icon="📑", layout="wide")

# ปรับแต่ง CSS เพื่อจัดทุกอย่างให้อยู่กึ่งกลางและดูพรีเมียม
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Sarabun:wght@300;400;700&display=swap');
    html, body, [class*="css"] {
        font-family: 'Sarabun', sans-serif;
    }
    .main {
        background-color: #0e1117;
    }
    .stButton>button {
        width: 100%;
        border-radius: 10px;
        height: 3em;
        background-color: #2e7d32;
        color: white;
        font-weight: bold;
        border: none;
    }
    .stDownloadButton>button {
        width: 100%;
        border-radius: 10px;
        background-color: #1565c0;
        color: white;
    }
    div[data-testid="stExpander"] {
        border: none;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
    }
    </style>
    """, unsafe_allow_html=True)

# ================= 1. ฟังก์ชันช่วยเหลือ (Utility) - คงเดิม =================
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

# ================= 2. Logic การอ่าน PDF - คงเดิม =================
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

# ================= 3. ส่วนการแสดงผล (New Centered UI) =================

# ใช้ columns เพื่อจัดวางทุกอย่างไว้กึ่งกลาง
col1, col2, col3 = st.columns([1, 2, 1])

with col2:
    st.markdown("<h1 style='text-align: center;'>📑 PDF to Excel Converter</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: gray;'>แปลงไฟล์ Statement เป็น Excel ง่ายๆ ในไม่กี่วินาที</p>", unsafe_allow_html=True)
    st.write("---")

    # ส่วนเลือกธนาคาร
    bank_option = st.selectbox(
        "🏦 เลือกธนาคาร",
        ("KBank (กสิกรไทย)", "SCB (ไทยพาณิชย์) - เร็วๆ นี้", "BBL (กรุงเทพ) - เร็วๆ นี้", "อื่นๆ"),
        help="ระบบกำลังพัฒนาให้รองรับธนาคารอื่นๆ เพิ่มเติมในเร็วๆ นี้"
    )

    # ส่วนอัปโหลดไฟล์
    pdf_file = st.file_uploader("📂 เลือกไฟล์ PDF Statement", type="pdf")
    
    # ส่วนรหัสผ่าน
    password = st.text_input("🔑 รหัสผ่านไฟล์ PDF (ถ้ามี)", type="password")

    # ปุ่มเริ่มแปลงไฟล์
    convert_button = st.button("เริ่มการแปลงไฟล์")

    # --- การประมวลผลหลังกดปุ่ม ---
    if convert_button and pdf_file:
        try:
            with st.spinner("กำลังประมวลผลข้อมูล..."):
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
                    with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='dd/mm/yyyy') as writer:
                        df.to_excel(writer, index=False, sheet_name='Statement')
                        workbook, worksheet = writer.book, writer.sheets['Statement']
                        
                        date_fmt = workbook.add_format({'num_format': 'dd/mm/yyyy', 'align': 'left'})
                        num_fmt = workbook.add_format({'num_format': '#,##0.00'})

                        worksheet.set_column('A:A', 12, date_fmt)
                        worksheet.set_column('D:E', 18, num_fmt)
                        worksheet.set_column('B:B', 10)
                        worksheet.set_column('C:C', 25)
                        worksheet.set_column('F:G', 45)
                    
                    output.seek(0)
                    
                    # 5. แสดงผลลัพธ์
                    st.success("✅ แปลงไฟล์สำเร็จเรียบร้อย!")
                    
                    # เพิ่ม Metrics สรุปสั้นๆ
                    total_in = df[df['ถอนเงิน/ฝากเงิน'] > 0]['ถอนเงิน/ฝากเงิน'].sum()
                    total_out = df[df['ถอนเงิน/ฝากเงิน'] < 0]['ถอนเงิน/ฝากเงิน'].sum()
                    
                    m1, m2 = st.columns(2)
                    m1.metric("ยอดรวมเงินเข้า", f"{total_in:,.2f} ฿")
                    m2.metric("ยอดรวมเงินออก", f"{abs(total_out):,.2f} ฿")

                    # ปุ่มดาวน์โหลด
                    st.download_button(
                        label="📥 ดาวน์โหลดไฟล์ Excel",
                        data=output,
                        file_name=f"Converted_{pdf_file.name.split('.')[0]}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )

                    with st.expander("ดูตัวอย่างข้อมูล 20 แถวแรก"):
                        st.dataframe(df.head(20), use_container_width=True)

        except PasswordError:
            st.error("❌ รหัสผ่านไม่ถูกต้อง กรุณาลองใหม่อีกครั้ง")
        except Exception as e:
            st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
    
    elif convert_button and not pdf_file:
        st.warning("⚠️ กรุณาเลือกไฟล์ PDF ก่อนกดปุ่ม")

# ท้ายหน้าเว็บ
st.write("---")
st.caption("<p style='text-align: center;'>Smart Statement Converter v2.0 | ข้อมูลของคุณปลอดภัยและไม่ถูกเก็บไว้ในเซิร์ฟเวอร์</p>", unsafe_allow_html=True)
