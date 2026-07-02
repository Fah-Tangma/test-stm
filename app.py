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

# ================= 3. ส่วนการแสดงผล (Sidebar UI) =================

st.title("📑 PDF Multi-Statement to Excel")
st.info("อัปโหลดไฟล์ PDF (สูงสุด 5 ไฟล์) ที่แถบด้านข้าง")

with st.sidebar:
    st.header("STM to Excel")
    
    bank_option = st.selectbox(
        "เลือกรูปแบบธนาคาร",
        ("กสิกรไทย (KBank)", "ไทยพาณิชย์ (SCB)", "กรุงไทย (KTB)", "กรุงเทพ (BBL)", "อื่น ๆ")
    )
    
    st.divider()

    st.write("**เลือกไฟล์ PDF (สูงสุด 5 ไฟล์)**")
    # ปรับตรงนี้ให้รับได้หลายไฟล์
    pdf_files = st.file_uploader("Upload", type="pdf", accept_multiple_files=True, label_visibility="collapsed")
    st.caption("Max 5 files • 200MB per file")

    st.write("**รหัสผ่านไฟล์ PDF (ถ้ามี)**")
    # หมายเหตุ: จะใช้รหัสผ่านนี้กับทุกไฟล์ที่อัปโหลด
    password = st.text_input("Password", type="password", label_visibility="collapsed")

    st.write("")
    convert_button = st.button("เริ่มการแปลงไฟล์")

# --- Logic การทำงานเมื่อกดปุ่ม ---
if convert_button:
    if pdf_files:
        if len(pdf_files) > 5:
            st.sidebar.error("❌ กรุณาอัปโหลดไม่เกิน 5 ไฟล์")
        else:
            try:
                all_dfs = []
                progress_bar = st.progress(0)
                
                for index, pdf_file in enumerate(pdf_files):
                    st.write(f"⏳ กำลังประมวลผลไฟล์: `{pdf_file.name}`...")
                    
                    # 1. ปลดล็อก PDF
                    pdf_bytes = pdf_file.read()
                    with pikepdf.open(io.BytesIO(pdf_bytes), password=password) as pdf:
                        unlocked_io = io.BytesIO()
                        pdf.save(unlocked_io)
                        unlocked_io.seek(0)
                        
                        # 2. อ่านข้อมูล
                        data_rows = parse_pdf_content(unlocked_io)
                        header = ["วันที่", "เวลา", "รายการ", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"]
                        temp_df = pd.DataFrame(data_rows, columns=header)
                        
                        # เพิ่มคอลัมน์ชื่อไฟล์เพื่อให้รู้ว่ามาจากไฟล์ไหน (Optional)
                        temp_df['Source File'] = pdf_file.name
                        all_dfs.append(temp_df)
                    
                    # อัปเดต Progress bar
                    progress_bar.progress((index + 1) / len(pdf_files))

                # 3. รวมข้อมูลทุกไฟล์
                final_df = pd.concat(all_dfs, ignore_index=True)
                
                # 4. จัดรูปแบบข้อมูล
                final_df['วันที่'] = pd.to_datetime(final_df['วันที่'], format='%d-%m-%y', errors='coerce')
                # เรียงลำดับตามวันที่และเวลา (Optional)
                final_df = final_df.sort_values(by=['วันที่', 'เวลา']).reset_index(drop=True)

                # 5. สร้าง Excel
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='mm/dd/yyyy') as writer:
                    final_df.to_excel(writer, index=False, sheet_name='Combined_Statement')
                    workbook, worksheet = writer.book, writer.sheets['Combined_Statement']
                    
                    date_fmt = workbook.add_format({'num_format': 'mm/dd/yyyy', 'align': 'left'})
                    num_fmt = workbook.add_format({'num_format': '_(* #,##0.00_);_(* (#,##0.00);_(* "-"??_);_(@_)'})

                    worksheet.set_column('A:A', 12, date_fmt)
                    worksheet.set_column('D:E', 18, num_fmt)
                    worksheet.set_column('B:B', 10)
                    worksheet.set_column('C:C', 25)
                    worksheet.set_column('F:G', 35)
                    worksheet.set_column('H:H', 20) # คอลัมน์ Source File
                
                output.seek(0)
                
                # 6. แสดงผล
                st.success(f"✅ แปลงไฟล์สำเร็จ รวมทั้งหมด {len(pdf_files)} ไฟล์ ({bank_option})")
                st.dataframe(final_df, use_container_width=True)
                
                st.download_button(
                    label="📥 ดาวน์โหลดไฟล์ Excel (รวมทุกไฟล์)",
                    data=output,
                    file_name="combined_statements.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True
                )

            except PasswordError:
                st.sidebar.error("❌ รหัสผ่านไม่ถูกต้อง (รหัสผ่านต้องเหมือนกันทุกไฟล์)")
            except Exception as e:
                st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
    else:
        st.sidebar.warning("⚠️ กรุณาเลือกไฟล์ PDF อย่างน้อย 1 ไฟล์")
