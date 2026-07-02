import io
import re
import os
import pandas as pd
import pikepdf
import pdfplumber
from flask import Flask, render_template, request, send_file, make_response
from pikepdf import PasswordError

app = Flask(__name__)
app.secret_key = "full_stm_converter_v4_final"

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

# ================= 2. Logic การอ่านและกรอง PDF (Filtering Logic ตามที่คุณกำหนด) =================

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

# ================= 3. Routes =================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/convert', methods=['POST'])
def convert():
    file = request.files.get('pdf_file')
    password = request.form.get('password', '').strip()

    if not file or file.filename == '':
        return "กรุณาเลือกไฟล์ PDF", 400

    download_name = f"{os.path.splitext(file.filename)[0]}.xlsx"

    try:
        pdf_bytes = io.BytesIO(file.read())
        with pikepdf.open(pdf_bytes, password=password) as pdf:
            unlocked_io = io.BytesIO()
            pdf.save(unlocked_io)
            unlocked_io.seek(0)
            
            data_rows = parse_pdf_content(unlocked_io)
            header = ["วันที่", "เวลา", "รายการ", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"]
            df = pd.DataFrame(data_rows, columns=header)
            
            df['วันที่'] = pd.to_datetime(df['วันที่'], format='%d-%m-%y', errors='coerce')

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
            
            # --- ส่งไฟล์พร้อมตั้งค่า Cookie เพื่อบอก JS ว่าเสร็จแล้ว ---
            response = make_response(send_file(output, as_attachment=True, download_name=download_name))
            response.set_cookie('download_started', 'true', path='/')
            return response

    except PasswordError:
        return "รหัสผ่านไม่ถูกต้อง", 401
    except Exception as e:
        return f"เกิดข้อผิดพลาด: {str(e)}", 500

if __name__ == "__main__":
    app.run(debug=True, port=8080)