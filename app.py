import io
import re
import json
import pandas as pd
import pikepdf
import pdfplumber
import streamlit as st
from pikepdf import PasswordError
from datetime import datetime
# --- สำหรับ Gemini ---
from google import genai
from google.genai import types
import os
import unicodedata

# ตั้งค่าหน้าเว็บ Streamlit
st.set_page_config(page_title="PDF Statement Converter", layout="wide")

# ================= 0. AI Configuration (สำหรับ BAY) =================
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]

def process_bay_with_gemini(file_bytes, password):
    """ฟังก์ชันจัดการไฟล์ BAY ด้วย Gemini AI"""
    client = genai.Client(api_key=GEMINI_API_KEY)
    unlocked_bytes = file_bytes
    try:
        with pikepdf.open(io.BytesIO(file_bytes), password=password) as pdf:
            out_pdf = io.BytesIO()
            pdf.save(out_pdf)
            unlocked_bytes = out_pdf.getvalue()
    except:
        pass

    model_name = "gemini-1.5-flash" 
    prompt = """
    คุณคือ OCR ผู้เชี่ยวชาญด้านบัญชี โปรดอ่านสเตทเมนท์ธนาคารกรุงศรี (BAY) จากไฟล์นี้
    และคืนค่าเป็น JSON Array ของ Array เท่านั้น [["วันที่", "เวลา", "จำนวนเงิน", "ยอดคงเหลือ", "รหัส", "รายละเอียด", "ช่องทาง", "รหัสสาขา"]]
    กฎเหล็ก:
    1. คอลัมน์ 'จำนวนเงิน': หากเป็นการ 'ถอน' ให้ติดลบ หากเป็น 'ฝาก' ให้เป็นบวก
    2. วันที่และเวลา: แยกออกจากกัน
    3. รายละเอียด: รวมข้อความคำอธิบายทั้งหมดให้อยู่ในบรรทัดเดียวกัน
    4. ห้ามมี Header ในข้อมูลที่ส่งกลับมา
    5. คืนค่าเฉพาะ JSON ห้ามมีคำอธิบายอื่น
    """
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=[types.Part.from_bytes(data=unlocked_bytes, mime_type="application/pdf"), prompt],
            config=types.GenerateContentConfig(response_mime_type='application/json'),
        )
        res_text = response.text.strip()
        if res_text.startswith("```"):
            res_text = res_text.replace("```json", "").replace("```", "").strip()
        return json.loads(res_text)
    except Exception as e:
        st.error(f"Gemini Error: {str(e)}")
        return None

# ================= 1. ฟังก์ชันช่วยเหลือทั่วไป =================
def str_to_float(val_str):
    if not val_str or str(val_str).strip() in ["", "-", "None"]: return 0.0
    try:
        clean_val = re.sub(r'[^\d.-]', '', str(val_str))
        return float(clean_val)
    except:
        return 0.0

def decode_cid(text):
    if not text: return ""
    cid_map = {"(cid:344)": "0", "(cid:345)": "1", "(cid:346)": "2", "(cid:347)": "3", "(cid:348)": "4",
               "(cid:349)": "5", "(cid:350)": "6", "(cid:351)": "7", "(cid:352)": "8", "(cid:353)": "9"}
    for cid, val in cid_map.items(): text = text.replace(cid, val)
    return text

def split_channel_and_detail(text):
    channels = ["EDC/K SHOP/MYQR", "โอนเข้า/หักบัญชีอัตโนมัติ", "K PLUS", "ตู้เติมเงิน / โมบาย แอปพลิ", 
                "Internet/Mobile KK", "K BIZ", "EDC", "ATM", "CDM", "BRANCH", "Internet/Mobile SCB", 
                "Internet/Mobile KTB", "Internet/Mobile BBL", "สาขาถนนศรีสุริยวงศ์", "สาขาเซ็นทรัล ขอนแก่น"]
    found_chan, detail = "-", text.strip()
    for c in channels:
        if c in text:
            found_chan = c
            detail = text.replace(c, "").strip().lstrip('/ ').strip()
            break
    return found_chan, detail

# ================= 2. ฟังก์ชันเฉพาะสำหรับ UOB (ตามที่คุณส่งมา) =================
def clean_description(text):
    replacements = {"MISCCREDIT": "MISC CREDIT", "MISCDEBIT": "MISC DEBIT", "PAYMENTEO": "PAYMENT EO",
                    "INVOICENO": "INVOICE NO", "INTERESTCREDIT": "INTEREST CREDIT", "WITHHOLDINGTAXDR": "WITHHOLDING TAX DR"}
    for old, new in replacements.items(): text = text.replace(old, new)
    return re.sub(r'\s+', ' ', text).strip()

def is_garbage_line(line):
    garbage_keywords = ["Account Statement", "Movement Details - From:", "Statement", "Value Date", "Transaction", 
                        "Description", "Deposit", "Withdrawal", "Balance", "Date/Time", "Total in Account Currency", 
                        "Note:", "-Balances and details reflected are indicative", "TotalinAccountCurrency"]
    line_upper = line.upper()
    if any(kw.upper() in line_upper for kw in garbage_keywords): return True
    if re.match(r'^\d+\s?/\s?\d+$', line): return True
    if re.match(r'^\d{2}/\d{2}/\d{4}$', line): return True
    return False

def parse_uob_pdf(pdf_stream):
    all_rows = []
    current_row = None
    date_pattern = r'(\d{2}/\d{2}/\d{4})'
    row_start_pattern = fr'^({date_pattern})\s+({date_pattern})\s+({date_pattern})'
    time_pattern = r'(\d{2}:\d{2}:\d{2}\s?(?:AM|PM))'
    with pdfplumber.open(pdf_stream) as pdf_obj:
        for page in pdf_obj.pages:
            text = page.extract_text()
            if not text: continue
            for line in text.split('\n'):
                line = line.strip()
                if not line or is_garbage_line(line): continue
                match_dates = re.match(row_start_pattern, line)
                if match_dates:
                    if current_row: all_rows.append(current_row)
                    amounts = re.findall(r'[\d,]+\.\d{2}', line)
                    current_row = {"st_date": match_dates.group(1), "val_date": match_dates.group(2), "tx_date": match_dates.group(3),
                                   "tx_time": "", "desc": "", "deposit": 0.0, "withdrawal": 0.0, "balance": 0.0}
                    if len(amounts) >= 3:
                        current_row["deposit"] = str_to_float(amounts[-3])
                        current_row["withdrawal"] = str_to_float(amounts[-2])
                        current_row["balance"] = str_to_float(amounts[-1])
                        desc_part = line[33:].strip().split(amounts[-3])[0].strip()
                        current_row["desc"] = desc_part
                elif current_row and re.search(time_pattern, line):
                    t_match = re.search(time_pattern, line)
                    current_row["tx_time"] = t_match.group(1)
                    current_row["desc"] += " " + line.replace(t_match.group(1), "").strip()
                elif current_row: current_row["desc"] += " " + line
        if current_row: all_rows.append(current_row)
    return all_rows

# ================= 3. Parsers อื่นๆ (KBank, SCB, KTB, BBL) =================
# ===== 1.KBank =====
def parse_kbank_pdf(pdf_stream):
    all_parsed_rows = []
    bf_keywords = ["ยอดยกมา", "Balance Brought Forward", "Brought Forward"]
    table_headers = ["เวลา/", "วันที่มีผล", "ถอนเงิน / ฝากเงิน", "ยอดคงเหลือ", "ทำรายการ (บาท)"]

    with pdfplumber.open(pdf_stream) as pdf_obj:
        for page in pdf_obj.pages:
            text = page.extract_text()
            if not text: continue
            
            lines = text.split('\n')
            is_in_table = False 

            for line in lines:
                line = line.strip()
                if not line: continue
                
                # --- 1. เช็ค Pattern วันที่ ---
                date_match = re.match(r'^(\d{2}-\d{2}-\d{2})', line)
                
                if date_match:
                    is_in_table = True 
                    date = date_match.group(1)
                    time_match = re.search(r'(\d{2}:\d{2})', line)
                    time = time_match.group(1) if time_match else ""
                    
                    # ปรับ Regex: r'-?[\d,]+\.\d{2}' เพื่อให้ดึงเครื่องหมายลบ (-) มาด้วย
                    amounts = re.findall(r'-?[\d,]+\.\d{2}', line)
                    
                    temp_text = line.replace(date, "", 1).strip()
                    if time: temp_text = temp_text.replace(time, "", 1).strip()
                    
                    desc = temp_text.split(amounts[0])[0].strip() if amounts else temp_text
                    
                    amount_val, balance = None, None
                    if len(amounts) == 1:
                        # กรณี 'ยอดยกมา' จะมีตัวเลขเดียว ซึ่งคือยอดคงเหลือ (อาจติดลบ)
                        balance = str_to_float(amounts[0])
                    elif len(amounts) >= 2:
                        # แยกยอดเงินเข้า/ออก
                        is_deposit = any(kw in desc for kw in ["รับเงิน", "คืนเงิน", "ฝาก", "เงินคืน", "Thai QR", "รับโอนเงิน", "รับโอน", "รับเงินจาก"])
                        val = str_to_float(amounts[0])
                        amount_val = val if is_deposit else -val
                        # ยอดคงเหลือคือตัวเลขชุดสุดท้ายในบรรทัด (อาจติดลบ)
                        balance = str_to_float(amounts[-1])

                    remaining = ""
                    if amounts:
                        parts = line.split(amounts[-1])
                        if len(parts) > 1: remaining = parts[-1].strip()
                    
                    chan, det = split_channel_and_detail(remaining)
                    all_parsed_rows.append([date, time, desc, amount_val, balance, chan, det])
                    continue 

                if any(kw in line for kw in table_headers):
                    is_in_table = True
                    continue
                
                if any(kw in line for kw in ["Total", "รวมทั้งสิ้น", "จบรายการ"]):
                    is_in_table = False
                    continue

                if is_in_table:
                    if any(x in line for x in ["หน้า", "แผ่นที่", "ยอดคงเหลือ", "รวมถอนเงิน", "รวมฝากเงิน"]): 
                        continue
                    c_extra, d_extra = split_channel_and_detail(line)
                    all_parsed_rows.append(["", "", "", None, None, c_extra if c_extra != "-" else "", d_extra])

    # =========================================================
    # ส่วนของการกรองข้อมูล (Filtering) - ปรับปรุงเพื่อไม่ให้ "ยอดยกมา" หาย
    # =========================================================
    
    rows_to_delete = set()
    n = len(all_parsed_rows)

    # --- เงื่อนไขที่ 1: จัดการรายการ "ยอดยกมา" (Brought Forward) ---
    bf_indices = [idx for idx, row in enumerate(all_parsed_rows) if any(kw in str(row[2]) for kw in bf_keywords)]
    
    if bf_indices:
        keep_idx = None
        # พยายามหาแถว "ยอดยกมา" ที่มีวันที่ (เพราะคือแถวที่อยู่ในตาราง)
        for idx in bf_indices:
            if all_parsed_rows[idx][0]: # index 0 คือ วันที่
                keep_idx = idx
                break
        
        # ถ้าหาแถวที่มีวันที่ไม่เจอเลย ให้เก็บแถวแรกที่เจอไว้
        if keep_idx is None:
            keep_idx = bf_indices[0]
            
        # สั่งลบแถว "ยอดยกมา" อื่นๆ ที่ไม่ใช่แถวที่เราเลือกจะเก็บ
        for idx in bf_indices:
            if idx != keep_idx:
                rows_to_delete.add(idx)

    # --- เงื่อนไขที่ 2: ลบกลุ่มแถวว่างที่ติดกันเกินไป (Noise) ---
    i = 0
    while i < n:
        # ตรวจสอบว่าเป็นแถวที่ไม่มีข้อมูลสำคัญ (วันที่ และ จำนวนเงิน)
        if all_parsed_rows[i][0] == "" and all_parsed_rows[i][3] is None:
            start_block = i
            while i < n and all_parsed_rows[i][0] == "" and all_parsed_rows[i][3] is None:
                i += 1
            end_block = i
            
            # หากเป็นแถวว่างติดกันเกิน 3 แถว สันนิษฐานว่าเป็นขยะจากหัว/ท้ายกระดาษ
            if (end_block - start_block) > 3:
                for k in range(start_block, end_block):
                    rows_to_delete.add(k)
        else:
            i += 1

    # สร้างผลลัพธ์สุดท้าย
    final_filtered_rows = [
        row for idx, row in enumerate(all_parsed_rows) 
        if idx not in rows_to_delete
    ]
            
    return final_filtered_rows

# ===== 2.SCB =====
# ===== 4.BBL =====
def clean_thai_text(text):
    if not text: return ""
    text = unicodedata.normalize('NFKC', text)
    # ลบช่องว่างกลางคำ
    text = re.sub(r'(?<=[ก-ฮ])\s+(?=[ะ-ูเ-โ])', '', text)
    text = re.sub(r'(?<=[ะ-ูเ-โ])\s+(?=[ก-ฮะ-์])', '', text)
    # แก้คำซ้ำ Artifact
    text = re.sub(r'([ก-ฮ][ะ-์])\1', r'\1', text) 
    text = re.sub(r'([ก-ฮ]{2})\1', r'\1', text)

    corrections = {
        "เงนิ": "เงิน", "เงิ น": "เงิน", "บญั": "บัญ", "บญัชี": "บัญชี", "อตั": "อัต",
        "โนมัตั ิ": "โนมัติ", "โนมตัิ": "โนมัติ", "โนมัติมัติ": "โนมัติ", "อตั โนมตั ิ": "อัตโนมัติ",
        "ตดั": "ตัด", "ดดั": "ตัด", "ตัดเตั": "ตัด", "เช็คอตั": "เช็คอัต", "ธรรมเน ยีม": "ธรรมเนียม",
        "คา่": "ค่า", "ไม่ผ่ าน": "ไม่ผ่าน", "ไม่ผ่ม่ าน": "ไม่ผ่าน", "ผ่ าน": "ผ่าน", "ผ่ม่ าน": "ผ่าน",
        "สะสมทรัพรั": "สะสมทรัพย์", "ทรัพรั ย์": "ทรัพย์", "ทรัพย": "ทรัพย์", "ปรบั": "ปรับ", "ปรับปรั รุง": "ปรับปรุง",
        "เป็ น": "เป็น", "ผ่ น": "ผ่าน", "ทํารายการ": "ทำรายการ", "ทีทำ": "ที่ทำ", "ทีมี": "ที่มี",
        "ไมผ่ า่ น": "ไม่ผ่าน", "ผา่ น": "ผ่าน", "ค่า ธรรมเนียม": "ค่าธรรมเนียม","ไม่ผ่านเป็น ผ่าน": "ไม่ผ่านเป็นผ่าน",
        "เป็น ": "เป็น",
        "ทรพัย์": "ทรัพย์"
    }
    for wrong, right in corrections.items():
        text = text.replace(wrong, right)
    return re.sub(r'\s+', ' ', text).strip()

def thai_date_to_eng(thai_date_str):
    months = {"ม.ค.": "01", "ก.พ.": "02", "มี.ค.": "03", "เม.ย.": "04", "พ.ค.": "05", "มิ.ย.": "06",
              "ก.ค.": "07", "ส.ค.": "08", "ก.ย.": "09", "ต.ค.": "10", "พ.ย.": "11", "ธ.ค.": "12"}
    try:
        parts = thai_date_str.split()
        if len(parts) == 3:
            d, m, y = parts[0].zfill(2), months.get(parts[1], "01"), str(int(parts[2]) - 543)
            return f"{d}/{m}/{y}"
    except: return None

def str_to_float(val):
    if not val: return 0.0
    try:
        if isinstance(val, str):
            val = val.replace(',', '')
        return float(val)
    except: return 0.0

# ================= 2. Logic การอ่านไฟล์ BBL =================

def parse_bbl_pdf(pdf_stream):
    all_rows = []
    date_pattern = r'(\d{1,2}\s+(?:ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.)\s+\d{4})'
    time_pattern = r'(\d{2}:\d{2})'
    
    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            lines = page.extract_text(x_tolerance=2, y_tolerance=2).split('\n')
            for i, line in enumerate(lines):
                line = line.strip()
                date_matches = re.findall(date_pattern, line)
                if not date_matches: continue
                if any(k in line for k in ["สรุปยอด", "รายการเคลื่อนไหว", "เลขที่บัญชี", "ยอดยกมา"]): continue

                time_val, extra_desc = "", ""
                time_in_line = re.search(time_pattern, line)
                if time_in_line: time_val = time_in_line.group(1)
                
                # อ่านบรรทัดถัดไปกรณีเวลาอยู่คนละบรรทัด
                if i + 1 < len(lines):
                    next_line = lines[i+1].strip()
                    time_match_next = re.search(r'^(\d{2}:\d{2})', next_line)
                    if time_match_next:
                        time_val = time_match_next.group(1)
                        extra_desc = next_line.replace(time_val, "").strip()

                amounts = re.findall(r'[\d,]+\.\d{2}', line)
                if not amounts: continue
                
                cheque_no = ""
                cheque_match = re.search(r'\b(\d{7,8})\b', line)
                if cheque_match: cheque_no = cheque_match.group(1)

                channel = ""
                chan_match = re.search(r'\b(BR\d+|DR\d+|AUTO|TELE|M-BANKING|INTERNET)\b', line)
                if chan_match: channel = chan_match.group(1)

                # ล้าง Text เพื่อเอาแค่ Description
                temp_desc = line
                for d_raw in date_matches: temp_desc = temp_desc.replace(d_raw, "")
                for amt in amounts: temp_desc = temp_desc.replace(amt, "")
                if channel: temp_desc = temp_desc.replace(channel, "")
                if cheque_no: temp_desc = temp_desc.replace(cheque_no, "")

                full_desc = clean_thai_text(temp_desc + " " + extra_desc)
                balance = str_to_float(amounts[-1])
                
                transaction_amount = 0.0
                if len(amounts) >= 2:
                    val = str_to_float(amounts[-2])
                    # แยกฝาก/ถอน
                    if any(word in full_desc for word in ["ฝาก", "เข้า", "รับโอน", "คืน", "ดอกเบี้ย"]):
                        transaction_amount = val
                    else:
                        transaction_amount = -val

                date_trans = thai_date_to_eng(date_matches[0])
                date_eff = thai_date_to_eng(date_matches[1]) if len(date_matches) > 1 else date_trans
                
                all_rows.append([date_trans, time_val, date_eff, full_desc, cheque_no, transaction_amount, balance, channel])
    return all_rows


# ===== 3.KTB =====
def parse_ktb_pdf(pdf_stream):
    all_raw_rows = []
    deposit_codes = ['IORSDT', 'IIPS', 'DDSDT', 'CR', 'OTHDEP', 'PBSDT', 'NBSDT']
    bf_keywords = ["ยอดยกมา", "ยอดคงเหลือยกมา", "Balance Brought Forward", "Brought Forward"]

    ignore_keywords = [
        "ธนาคารกรุงไทย", "หน้า", "รายการเดินบัญชี", "ชื่อบัญชี", "ประเภทบัญชี",
        "เลขที่บัญชี", "รหัสสาขา", "ที่อยู่ปัจจุบัน", "ที่อยู่สาขา", "วงเงินเบิกเกินบัญชี",
        "สกุลเงิน", "ติดต่อ เบอร์", "อีเมล", "Krungthai Bank", "Statement", 
        "รวมรายการ", "เลขที่", "บริษัท ธนาคารกรุงไทย",
        "ถนนสุขุมวิท", "แขวงคลองเตยเหนือ", "เขตวัฒนา", "กรุงเทพฯ", 
        "Krungthai Corporate Call Center", "02-111-9999", 
        "cash.management@krungthai.com", "www.krungthai.com"
    ]

    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text: continue
            text = decode_cid(text) 
            lines = text.split('\n')
            last_idx = -1
            
            for line in lines:
                line = line.strip()
                if not line: continue

                # --- 0. ตรวจสอบ Ignore Keywords ---
                if any(kw in line for kw in ignore_keywords) and not re.search(r'\d+\.\d{2}', line):
                    continue

                # --- 1. ตรวจสอบ "ยอดยกมา" (B/F) ---
                if any(kw in line for kw in bf_keywords):
                    amts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', line)
                    date_match = re.search(r'(\d{2}/\d{2}/\d{2,4})', line)
                    d_val = date_match.group(1) if date_match else ""
                    if amts:
                        balance_val = str_to_float(amts[-1])
                        all_raw_rows.append([d_val, "", "B/F", "ยอดยกมา", 0.0, 0.0, balance_val, "-"])
                        last_idx = len(all_raw_rows) - 1
                        continue

                # --- 2. รูปแบบ Biz Format (ปี ค.ศ. YYYY เช่น 30/06/2026) ---
                biz_match = re.match(r'^(\d{2}/\d{2}/\d{4})\s*(\d{2}:\d{2})?\s*([A-Z0-9]+)\s+(.*)', line)
                if biz_match:
                    d, t, c, rem = biz_match.groups()
                    t = t if t else ""
                    amts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', rem)
                    
                    f_amt, tax_amt, balance_val = 0.0, 0.0, 0.0
                    if len(amts) >= 3:
                        # Biz Format: ตัวเลข 3 ชุดคือ [จำนวนเงิน, ภาษี, ยอดคงเหลือ]
                        val_raw = str_to_float(amts[0])
                        tax_amt = -abs(str_to_float(amts[1]))
                        balance_val = str_to_float(amts[-1])
                        f_amt = val_raw if any(dc in c for dc in deposit_codes) else -val_raw
                    elif len(amts) == 2:
                        val_raw = str_to_float(amts[0])
                        balance_val = str_to_float(amts[-1])
                        tax_amt = 0.0
                        f_amt = val_raw if any(dc in c for dc in deposit_codes) else -val_raw
                    
                    detail = rem.split(amts[0])[0].strip() if amts else rem
                    branch = rem.split(amts[-1])[-1].strip() if amts else "Krungthai Business"
                    
                    all_raw_rows.append([d, t, c, detail, f_amt, tax_amt, balance_val, branch])
                    last_idx = len(all_raw_rows) - 1
                    continue
                    
                # --- 3. รูปแบบ Personal Format (ปี พ.ศ./ค.ศ. YY เช่น 30/06/26) ---
                pers_match = re.match(r'^(\d{2}/\d{2}/\d{2})\s*(.*?)\s*\(([A-Z]+)\)\s*(.*)', line)
                if pers_match:
                    d, name, c, rem = pers_match.groups()
                    amts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', rem)
                    
                    f_amt, tax_amt, balance_val = 0.0, 0.0, 0.0
                    if len(amts) >= 3:
                        # Personal Format: ตัวเลข 3 ชุดคือ [ถอนเงิน, ฝากเงิน, ยอดคงเหลือ]
                        w_amt = str_to_float(amts[0])
                        d_amt = str_to_float(amts[1])
                        balance_val = str_to_float(amts[-1])
                        
                        if d_amt > 0 and w_amt == 0:
                            f_amt = d_amt
                        elif w_amt > 0:
                            f_amt = -w_amt
                        else:
                            f_amt = d_amt if d_amt > 0 else -w_amt
                    elif len(amts) == 2:
                        raw = str_to_float(amts[0])
                        balance_val = str_to_float(amts[-1])
                        f_amt = raw if (c in deposit_codes or "เข้า" in name) else -raw
                    
                    # บุคคลธรรมดาไม่มีช่องภาษีแยก -> ตั้งภาษีเป็น 0.0 เสมอ
                    tax_amt = 0.0
                    
                    detail = rem.split(amts[0])[0].strip() if amts else rem
                    branch = line.split()[-1] if line.split() else "Krungthai Personal"
                    
                    all_raw_rows.append([d, "", f"{name} ({c})", detail, f_amt, tax_amt, balance_val, branch])
                    last_idx = len(all_raw_rows) - 1
                    continue

                # --- 4. บรรทัดรายละเอียดเพิ่มเติม หรือ เวลา ---
                time_row_match = re.match(r'^(\d{2}:\d{2})(.*)', line)
                if time_row_match and last_idx != -1:
                    all_raw_rows[last_idx][1] = time_row_match.group(1)
                    if time_row_match.group(2):
                        all_raw_rows[last_idx][3] += " " + time_row_match.group(2).strip()
                elif last_idx != -1:
                    if not re.match(r'^\d{2}/\d{2}/', line):
                        all_raw_rows.append(["", "", "", line, None, None, None, ""])
                        
    # ================= 5. Filtering Process (ลบยอดยกมาซ้ำ และ ลบแถวว่าง > 1) =================

    # ขั้นตอนที่ 5.1: ลบ "ยอดยกมา" (B/F) ให้เหลือแค่แถวแรกสุดตัวเดียว
    temp_list_bf = []
    found_first_bf = False
    for row in all_raw_rows:
        if row[2] == "B/F":
            if not found_first_bf:
                temp_list_bf.append(row)
                found_first_bf = True
        else:
            temp_list_bf.append(row)

    # ขั้นตอนที่ 5.2: ลบแถวว่าง (Amount is None) ที่ต่อเนื่องกันมากกว่า 1 แถว
    final_filtered_rows = []
    i, n = 0, len(temp_list_bf)
    while i < n:
        # ถ้าแถวนั้นมีจำนวนเงิน หรือเป็นยอดยกมาที่เลือกไว้ ให้เก็บไว้
        if temp_list_bf[i][4] is not None or temp_list_bf[i][2] == "B/F":
            final_filtered_rows.append(temp_list_bf[i])
            i += 1
        else:
            # เริ่มตรวจสอบกลุ่มแถวว่าง
            empty_block = []
            while i < n and temp_list_bf[i][4] is None and temp_list_bf[i][2] != "B/F":
                # กรองพวกคำใน ignore_keywords อีกครั้งเพื่อความชัวร์
                if not any(kw in str(temp_list_bf[i][3]) for kw in ignore_keywords):
                    empty_block.append(temp_list_bf[i])
                i += 1
            
            # ถ้ามีแถวว่างแถวเดียว (มักจะเป็นรายละเอียดต่อท้าย) ให้เอาไป Merge กับแถวบน
            if len(empty_block) == 1:
                if final_filtered_rows:
                    final_filtered_rows[-1][3] = (str(final_filtered_rows[-1][3]) + " " + str(empty_block[0][3])).strip()
            # ถ้ามีมากกว่า 1 แถว ให้ "ลบทิ้งทั้งหมด" (ข้ามไปเลย)

    return final_filtered_rows

# ===== 4.BBL =====
import re
import pdfplumber
import unicodedata

def clean_thai_text(text):
    if not text: return ""
    text = unicodedata.normalize('NFKC', text)
    # ลบตัวซ้อน Artifact ของ BBL (เช่น มิ.มิย. -> มิ.ย.)
    text = re.sub(r'([ก-ฮ]\.[ก-ฮ]\.)\1', r'\1', text)
    text = re.sub(r'([ก-ฮ])[\u0e30-\u0e4c]\1', r'\1', text) 
    
    # แก้คำผิดเฉพาะจุด
    corrections = {
        "มิ.มิย.": "มิ.ย.", "เม.เมย.": "เม.ย.", "พ.พ.ค.": "พ.ค.",
        "เงนิ": "เงิน", "บญั": "บัญ", "คา่": "ค่า", "ตดั": "ตัด"
    }
    for wrong, right in corrections.items():
        text = text.replace(wrong, right)
    
    return re.sub(r'\s+', ' ', text).strip()

def thai_date_to_eng(thai_date_str):
    if not thai_date_str: return None
    months = {"ม.ค.": "01", "ก.พ.": "02", "มี.ค.": "03", "เม.ย.": "04", "พ.ค.": "05", "มิ.ย.": "06",
              "ก.ค.": "07", "ส.ค.": "08", "ก.ย.": "09", "ต.ค.": "10", "พ.ย.": "11", "ธ.ค.": "12"}
    try:
        # ตัดเศษอักษรหลังปี เช่น "2569 มิ" -> "2569"
        thai_date_str = re.sub(r'(\d{4}).*', r'\1', thai_date_str)
        parts = thai_date_str.split()
        if len(parts) >= 3:
            d = parts[0].zfill(2)
            m = months.get(parts[1], "01")
            y = str(int(parts[2]) - 543)
            return f"{d}/{m}/{y}"
    except: return None

def str_to_float(val):
    if not val: return 0.0
    try:
        return float(str(val).replace(',', ''))
    except: return 0.0

# ================= Logic การอ่านไฟล์ BBL ที่ปรับปรุงใหม่ =================

def parse_bbl_pdf(pdf_stream):
    all_rows = []
    # Regex ที่ยืดหยุ่นขึ้นเพื่อดักจับวันที่แม้มีตัวอักษรซ้ำ
    date_pattern = r'(\d{1,2}\s+[ก-ธ\.\s]{3,10}\s+\d{4})'
    time_pattern = r'(\d{2}:\d{2})'
    amount_pattern = r'[\d,]+\.\d{2}'

    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            # ใช้ y_tolerance=5 เพื่อรวมบรรทัดที่สายตาเราเห็นเป็นบรรทัดเดียวกันแต่ PDF แยกกัน
            lines = page.extract_text(x_tolerance=3, y_tolerance=5).split('\n')
            
            row_buffer = None

            for line in lines:
                line = line.strip()
                if not line: continue

                # ตรวจสอบว่าเป็นบรรทัดเริ่มต้นรายการใหม่ (มีวันที่)
                date_matches = re.findall(date_pattern, line)
                
                if date_matches:
                    # ถ้ามีข้อมูลเก่าค้างใน Buffer ให้บันทึกก่อนเริ่มรายการใหม่
                    if row_buffer:
                        process_buffer(row_buffer, all_rows)
                    
                    row_buffer = {
                        'dates': date_matches,
                        'text': line,
                        'amounts': re.findall(amount_pattern, line),
                        'time': ""
                    }
                    # หาเวลาในบรรทัดเดียวกัน
                    time_search = re.search(time_pattern, line)
                    if time_search: row_buffer['time'] = time_search.group(1)
                
                elif row_buffer:
                    # ถ้าไม่มีวันที่ แต่มี Buffer ค้างอยู่ (แปลว่าเป็นบรรทัดรายละเอียดหรือเวลาของรายการก่อนหน้า)
                    row_buffer['text'] += " " + line
                    # หาตัวเลขเพิ่มเติม (เช่น ยอดคงเหลือที่อาจอยู่คนละบรรทัด)
                    row_buffer['amounts'].extend(re.findall(amount_pattern, line))
                    # หาเวลาถ้ายังไม่มี
                    if not row_buffer['time']:
                        time_search = re.search(time_pattern, line)
                        if time_search: row_buffer['time'] = time_search.group(1)

            # บันทึกรายการสุดท้ายของหน้า
            if row_buffer:
                process_buffer(row_buffer, all_rows)

    return all_rows

def process_buffer(buffer, all_rows):
    """ทำความสะอาดและจัดระเบียบข้อมูลจาก Buffer ก่อนเพิ่มลง List"""
    desc = clean_thai_text(buffer['text'])
    
    # ลบข้อมูลวันที่ออกจากรายละเอียดเพื่อความสะอาด
    for d in buffer['dates']:
        desc = desc.replace(d, "")
    
    # ลบตัวเลขเงินออกจากรายละเอียด
    unique_amounts = []
    for a in buffer['amounts']:
        if a not in unique_amounts: unique_amounts.append(a)
        desc = desc.replace(a, "")

    # จัดการจำนวนเงิน
    amounts_float = [str_to_float(a) for a in unique_amounts]
    
    if len(amounts_float) >= 1:
        balance = amounts_float[-1] # ตัวสุดท้ายคือยอดคงเหลือเสมอ
        trans_amount = amounts_float[-2] if len(amounts_float) >= 2 else 0.0
        
        # ค้นหาเลขเช็ค
        cheque_no = ""
        cheque_match = re.search(r'\b(\d{7,8})\b', buffer['text'])
        if cheque_match: cheque_no = cheque_match.group(1)

        # ค้นหาช่องทาง
        channel = ""
        chan_match = re.search(r'\b(BR\d+|AUTO|INTERNET|M-BANKING|DR\d+)\b', buffer['text'])
        if chan_match: channel = chan_match.group(1)

        # แยก ฝาก/ถอน (ดูจากคำสำคัญ)
        if any(word in desc for word in ["ฝาก", "เข้า", "รับโอน", "คืน", "ดอกเบี้ย", "Clearing"]):
            actual_amount = trans_amount
        else:
            actual_amount = -trans_amount

        # แปลงวันที่
        date_trans = thai_date_to_eng(buffer['dates'][0])
        date_eff = thai_date_to_eng(buffer['dates'][1]) if len(buffer['dates']) > 1 else date_trans

        all_rows.append([date_trans, buffer['time'], date_eff, desc.strip(), cheque_no, actual_amount, balance, channel])

# ================= 4. Streamlit UI & Logic =================
st.title("📑 PDF Statement to Excel")

with st.sidebar:
    st.header("ตัวเลือก")
    bank_option = st.selectbox("เลือกธนาคาร", ["กสิกรไทย (KBank)", "ไทยพาณิชย์ (SCB)", "กรุงไทย (KTB)", "กรุงศรี (BAY)", "กรุงเทพ (BBL)", "ยูโอบี (UOB)"])
    pdf_files = st.file_uploader("เลือกไฟล์ PDF", type="pdf", accept_multiple_files=True)
    password = st.text_input("รหัสผ่านไฟล์ (ถ้ามี)", type="password")
    convert_button = st.button("เริ่มการแปลงไฟล์", use_container_width=True)

if convert_button:
    if not pdf_files:
        st.error("⚠️ กรุณาเลือกไฟล์ PDF")
    else:
        all_dfs = []
        status_placeholder = st.empty()
        
        try:
            for i, uploaded_file in enumerate(pdf_files):
                status_placeholder.write(f"⏳ กำลังประมวลผล: {uploaded_file.name}...")
                pdf_bytes = uploaded_file.read()
                
                # --- กรณี BAY (AI) ---
                if bank_option == "กรุงศรี (BAY)":
                    data_rows = process_bay_with_gemini(pdf_bytes, password)
                    if data_rows:
                        df = pd.DataFrame(data_rows, columns=["วันที่", "เวลา", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "รหัส", "รายละเอียด", "ช่องทาง", "รหัสสาขา"])
                        df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True, errors='coerce')
                        all_dfs.append(df)
                
                # --- กรณีธนาคารอื่นๆ (Rule-based) ---
                else:
                    with pikepdf.open(io.BytesIO(pdf_bytes), password=password) as pdf:
                        unlocked_io = io.BytesIO()
                        pdf.save(unlocked_io)
                        unlocked_io.seek(0)
                        
                        if bank_option == "กสิกรไทย (KBank)":
                            rows = parse_kbank_pdf(unlocked_io)
                            df = pd.DataFrame(rows, columns=["วันที่", "เวลา", "รายการ", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"])
                            df['วันที่'] = pd.to_datetime(df['วันที่'], format='%d-%m-%y', errors='coerce')
                        
                        elif bank_option == "ไทยพาณิชย์ (SCB)":
                            rows = parse_scb_pdf(unlocked_io)
                            df = pd.DataFrame(rows, columns=["วันที่", "เวลา", "รายการ", "ช่องทาง", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "รายละเอียด"])
                            df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True, errors='coerce')
                        
                        elif bank_option == "กรุงไทย (KTB)":
                            rows = parse_ktb_pdf(unlocked_io)
                            df = pd.DataFrame(rows, columns=["วันที่", "เวลา", "รายการ", "รายละเอียด", "ถอนเงิน/ฝากเงิน", "ภาษี", "ยอดคงเหลือ", "สาขา"])
                            df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True, errors='coerce')
                        
                        elif bank_option == "กรุงเทพ (BBL)":
                            rows = parse_bbl_pdf(unlocked_io)
                            rows.reverse() 
                            df = pd.DataFrame(rows, columns=["วันที่", "เวลา", "วันที่ที่มีผล", "รายละเอียด", "เลขที่เช็ค", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "ช่องทาง"])
                            df['วันที่'] = pd.to_datetime(df['วันที่'], format='%d/%m/%Y', errors='coerce')
                        
                        elif bank_option == "ยูโอบี (UOB)":
                            raw_uob = parse_uob_pdf(unlocked_io)
                            # สร้างข้อมูลตามหัวตารางที่กำหนด
                            uob_data = [[
                                r["st_date"], r["val_date"], r["tx_date"], 
                                r["tx_time"], clean_description(r["desc"]), 
                                (r["deposit"] - r["withdrawal"]), r["balance"]
                            ] for r in raw_uob]
                            
                            df = pd.DataFrame(uob_data, columns=[
                                "Statement Date", "Value Date", "Transaction Date", 
                                "Transaction Time", "Description", "Deposit/Withdrawal", "Balance"
                            ])
                            # แปลงวันที่สำหรับ UOB
                            df['Statement Date'] = pd.to_datetime(df['Statement Date'], format='%d/%m/%Y', errors='coerce')
                            df['Value Date'] = pd.to_datetime(df['Value Date'], format='%d/%m/%Y', errors='coerce')
                            df['Transaction Date'] = pd.to_datetime(df['Transaction Date'], format='%d/%m/%Y', errors='coerce')
                        
                        all_dfs.append(df)

            if all_dfs:
                final_df = pd.concat(all_dfs, ignore_index=True)
                st.dataframe(final_df, use_container_width=True)

                # --- Export Excel ---
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='m/d/yyyy') as writer:
                    final_df.to_excel(writer, index=False, sheet_name='Statement')
                    workbook = writer.book
                    worksheet = writer.sheets['Statement']

                    # สีตามธนาคาร
                    colors = {
                        "กสิกรไทย (KBank)": '#00A950', 
                        "ไทยพาณิชย์ (SCB)": '#4E2E7F', 
                        "กรุงไทย (KTB)": '#00A1E0', 
                        "กรุงศรี (BAY)": '#FFCC00', 
                        "กรุงเทพ (BBL)": '#0A22A8', 
                        "ยูโอบี (UOB)": '#003399'
                    }
                    h_color = colors.get(bank_option, '#333333')
                    # กำหนดสีฟอนต์ (BAY ใช้สีดำ, อื่นๆ ใช้สีขาว)
                    f_color = 'black' if bank_option == "กรุงศรี (BAY)" else 'white'
                    
                    header_fmt = workbook.add_format({'bold': True, 'bg_color': h_color, 'font_color': f_color, 'align': 'center', 'border': 1})
                    num_fmt = workbook.add_format({'num_format': '_(* #,##0.00_);_(* (#,##0.00);_(* "-"??_);_(@_)', 'align': 'right', 'valign': 'vcenter'})
                    date_fmt = workbook.add_format({'num_format': 'm/d/yyyy', 'align': 'left'})
                    
                    # เขียน Header
                    for col_num, value in enumerate(final_df.columns.values):
                        worksheet.write(0, col_num, value, header_fmt)
                    
                    worksheet.set_column('A:Z', 18)
                    
                    # จัด Format วันที่ (คอลัมน์ที่มีคำว่า Date หรือ วันที่)
                    for idx, col_name in enumerate(final_df.columns):
                        if "Date" in col_name or "วันที่" in col_name:
                            worksheet.set_column(idx, idx, 15, date_fmt)
                        # จัด Format ตัวเลข
                        if any(kw in col_name for kw in ["ถอนเงิน", "ฝากเงิน", "ยอดคงเหลือ", "จำนวนเงิน", "ภาษี", "Deposit/Withdrawal", "Balance"]):
                            worksheet.set_column(idx, idx, 15, num_fmt)

                output.seek(0)
                st.download_button(
                    label="📥 ดาวน์โหลดไฟล์ Excel", 
                    data=output, 
                    file_name=f"Statement_{bank_option}_{datetime.now().strftime('%Y%m%d')}.xlsx"
                )
                status_placeholder.success("✅ แปลงไฟล์สำเร็จ!")

        except PasswordError:
            st.error("❌ รหัสผ่านไม่ถูกต้อง")
        except Exception as e:
            st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
