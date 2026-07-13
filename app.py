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
def str_to_float(val):
    if not val or not isinstance(val, str): return 0.0
    return float(val.replace(',', ''))

def parse_scb_pdf(pdf_stream):
    all_parsed_rows = []
    header_found = False
    pending_desc = ""

    # คีย์เวิร์ดสำหรับยอดยกมา (ใช้ตัวใหญ่ทั้งหมดเพื่อเทียบ .upper())
    bf_keywords = ["ยอดยกมา", "BALANCE BROUGHT FORWARD", "ยอดเงินคงเหลือยกมา"]
    
    table_headers = [
        "Date", "Time", "Code", "Channel", "Cheque No.", "Withdrawal", "Deposit", "Description",
        "Debit/Credit", "Balance/Baht", "วันที่", "เวลา", "รายการ", "ช่องทาง", "ยอดเงินคงเหลือ"
    ]

# รวมคำที่ไม่สนใจทั้งหมด (หัวกระดาษ, ท้ายกระดาษ, ข้อมูลบริษัท, Disclaimer)
    ignore_keywords = table_headers + [
        "This document is auto-generated", "signature is not required", 
        "THE SIAM COMMERCIAL BANK PUBLIC COMPANY LIMITED", "สาขา ASAWANN SHOPPING COMPLEX",
        "บริษัท เอสพี ริช กรุ๊ป จำกัด", "STATEMENT OF SAVING ACCOUNT", 
        "เลขที่บัญชี", "ที่อยู่", "Account No.", "Address", "Name", "ชื่อ - สกุล",
        "TOTAL ITEMS", "TOTAL AMOUNT", "TOTAL DEBIT", "TOTAL CREDIT",
        "กรุณาติดต่อศูนย์บริการลูกค้าธุรกิจ", "02-722-2222", "Contact Center",
        "computer-generated", "authorized person", "signature of SCB",
        "หน้าที่", "Page", "เอกสารฉบับนี้", "จัดพิมพ์ผ่านระบบคอมพิวเตอร์",
        "Balance Carried Forward", "ยอดเงินคงเหลือยกไป", "ธนาคารไทยพาณิชย์", "จำกัด", "(มหาชน)", "จำนวนเงินนำเข้าบัญชีทั้งหมด", 
        "Total Credit Amount", "จำนวนเงินที่หักบัญชีทั้งหมด", "Total Debit Amount"
    ]

    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text: continue
            lines = text.split('\n')
            
            for line in lines:
                line_clean = line.strip()
                if not line_clean: continue

                # --- 1. เช็คยอดยกมา (BF) เป็นอันดับแรก ---
                if any(kw.upper() in line_clean.upper() for kw in bf_keywords):
                    amounts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', line_clean)
                    if amounts:
                        # ยอดยกมามักจะเป็นยอดเงินสุดท้ายของบรรทัดนี้
                        balance = str_to_float(amounts[-1])
                        all_parsed_rows.append([None, None, "B/F", "-", 0.0, balance, "ยอดยกมา (BALANCE BROUGHT FORWARD)"])
                    header_found = True # เมื่อเจอยอดยกมาแล้ว ถือว่าเริ่มตารางแล้ว
                    continue

                # --- 2. เช็คหัวตาราง เพื่อเริ่มอ่านข้อมูลในหน้าใหม่ๆ ---
                if ("Date" in line_clean and "Time" in line_clean) or ("วันที่" in line_clean and "เวลา" in line_clean):
                    header_found = True
                    continue 

                if not header_found:
                    continue

                # --- 3. ข้ามบรรทัดที่ไม่ใช่ข้อมูล (Header ซ้ำ/Footer) ---
                if any(kw in line_clean for kw in ignore_keywords):
                    continue

                # --- 4. อ่านรายการ Transaction ---
                # Regex ตรวจวันที่ (DD/MM/YY หรือ DD/MM/YYYY) และ เวลา (HH:MM)
                transaction_match = re.match(r'^(\d{2}/\d{2}/\d{2,4})\s+(\d{2}:\d{2})', line_clean)
                
                if transaction_match:
                    date_str = transaction_match.group(1)
                    time_str = transaction_match.group(2)
                    
                    amounts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', line_clean)
                    
                    temp_text = line_clean.replace(date_str, "").replace(time_str, "").strip()
                    parts = temp_text.split()
                    
                    code = parts[0] if len(parts) > 0 else "-"
                    # ตรวจสอบว่าช่อง Channel มีข้อมูลไหม (ถ้าตัวถัดไปไม่ใช่ตัวเลขยอดเงิน)
                    channel = parts[1] if len(parts) > 1 and not re.match(r'[\d,]+\.\d{2}', parts[1]) else "-"
                    
                    amount_val, balance_val = 0.0, 0.0
                    if len(amounts) >= 2:
                        balance_val = str_to_float(amounts[-1])
                        raw_amount = str_to_float(amounts[-2])
                        
                        # แยกเงินเข้า (+) หรือเงินออก (-) ตาม Code
                        # รหัสเงินเข้าพบบ่อย: X1, IN, IT, BT, DP, CR, SD, C1
                        credit_codes = ['X1', 'IN', 'IT', 'BT', 'DP', 'CR', 'SD', 'C1', 'NR', 'TRN']
                        if code.upper() in credit_codes:
                            amount_val = raw_amount
                        else:
                            # รหัสเงินออกพบบ่อย: FE, WD, ATM, TR, DC, X2 (บางกรณี)
                            amount_val = -raw_amount
                    elif len(amounts) == 1:
                        balance_val = str_to_float(amounts[0])

                    # ตัดส่วนวันที่ เวลา รหัส และยอดเงินออก เพื่อให้เหลือแต่ Description
                    desc_raw = line_clean.replace(date_str, "").replace(time_str, "").replace(code, "", 1)
                    if channel != "-": desc_raw = desc_raw.replace(channel, "", 1)
                    for amt in amounts: desc_raw = desc_raw.replace(amt, "")
                    
                    final_desc = (pending_desc + " " + desc_raw.strip()).strip()
                    pending_desc = "" 
                    
                    all_parsed_rows.append([date_str, time_str, code, channel, amount_val, balance_val, final_desc])
                
                # --- 5. เก็บรายละเอียดที่อยู่คนละบรรทัด ---
                elif all_parsed_rows:
                    # ถ้าเจอคำหลักที่เป็นจุดเริ่มรายละเอียด
                    keywords_desc = ("รับโอนจาก", "โอนไป", "รับเงินโอน", "ชำระเงิน", "จากระบบ", "ค่าธรรมเนียม", "PromptPay", "TO ", "FROM ")
                    if line_clean.startswith(keywords_desc):
                        pending_desc = (pending_desc + " " + line_clean).strip()
                    else:
                        # กรณีเป็นข้อความรายละเอียดทั่วไป ให้ต่อท้ายรายการล่าสุด
                        all_parsed_rows[-1][6] = (all_parsed_rows[-1][6] + " " + line_clean).strip()

     # --- ส่วนของการกรองข้อมูล (คงโครงสร้างเดิมตามที่คุณต้องการ) ---
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
                # ถ้าเจอรายการยอดยกมาในบล็อกว่าง ให้เก็บไว้
                if any(kw in str(temp_list_bf[i][2]) for kw in bf_keywords):
                    final_filtered_rows.append(temp_list_bf[i])
                    i += 1
                    continue
                empty_block.append(temp_list_bf[i])
                i += 1
            # รวบรายละเอียดเสริม (ถ้ามีมากกว่า 1 บรรทัดก็ยังคงนำไปแสดงผล)
            for item in empty_block:
                final_filtered_rows.append(item)
            
    return final_filtered_rows

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
def process_bbl_with_gemini(file_bytes, password):
    """ฟังก์ชันจัดการไฟล์ BBL ด้วย Gemini AI พร้อมแปลงวันที่เป็นตัวเลข"""
    client = genai.Client(api_key=GEMINI_API_KEY)
    unlocked_bytes = file_bytes
    try:
        with pikepdf.open(io.BytesIO(file_bytes), password=password) as pdf:
            out_pdf = io.BytesIO()
            pdf.save(out_pdf)
            unlocked_bytes = out_pdf.getvalue()
    except:
        pass

    model_name = "gemini-2.5-flash" 
    prompt = """
    คุณคือ OCR ผู้เชี่ยวชาญด้านบัญชี โปรดอ่านสเตทเมนท์ธนาคารกรุงเทพ (BBL) นี้
    และคืนค่าเป็น JSON Array ของ Array เท่านั้น โดยมีลำดับคอลัมน์ดังนี้:
    [["วันที่ทำรายการ", "เวลา", "วันที่มีผล", "รายละเอียด", "เลขที่เช็ค", "จำนวนเงิน", "ยอดคงเหลือ", "ช่องทาง"]]

    กฎเหล็ก:
    1. **วันที่**: ให้แปลงวันที่จากรูปแบบ '26 มิ.ย. 2569' เป็น '26/06/2026' (ค.ศ.) เสมอ 
       - ใช้เกณฑ์: ม.ค.=01, ก.พ.=02, มี.ค.=03, เม.ย.=04, พ.ค.=05, มิ.ย.=06, ก.ค.=07, ส.ค.=08, ก.ย.=09, ต.ค.=10, พ.ย.=11, ธ.ค.=12
       - ปี พ.ศ. 2569 ให้แปลงเป็น ค.ศ. 2026
    2. **เติมวันที่ให้ครบ**: แม้ใน PDF วันที่ทำรายการจะเขียนไว้บรรทัดเดียวสำหรับหลายรายการ แต่ใน JSON ต้องใส่ข้อมูลวันที่ให้ครบทุกแถว (ห้ามเป็นค่าว่าง)
    3. **จำนวนเงิน**: ช่อง 'หักบัญชี' ให้ติดลบ, ช่อง 'เข้าบัญชี' ให้เป็นบวก
    4. **รายละเอียด**: รวมข้อความคำอธิบายทั้งหมดให้อยู่ในบรรทัดเดียวกัน
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
        
        data = json.loads(res_text)
        return data
    except Exception as e:
        st.error(f"Gemini Error (BBL): {str(e)}")
        return None

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
                df = None  # กำหนดค่าเริ่มต้นเพื่อป้องกัน Error 'df' is not defined

                # --- 1. กลุ่มธนาคารที่ใช้ AI (BAY และ BBL) ---
                if bank_option == "กรุงศรี (BAY)":
                    data_rows = process_bay_with_gemini(pdf_bytes, password)
                    if data_rows:
                        df = pd.DataFrame(data_rows, columns=["วันที่", "เวลา", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "รหัส", "รายละเอียด", "ช่องทาง", "รหัสสาขา"])
                        df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True, errors='coerce')
                elif bank_option == "กรุงเทพ (BBL)":
                    data_rows = process_bbl_with_gemini(pdf_bytes, password)
                    if data_rows:
                        # 1. สร้าง DataFrame
                        df = pd.DataFrame(data_rows, columns=["วันที่", "เวลา", "วันที่มีผล", "รายละเอียด", "เลขที่เช็ค", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "ช่องทาง"])
                        
                        # 2. ลบแถวขยะที่อาจติดมา
                        df = df[df['เวลา'] != 'เวลา'] 
                
                        # 3. จัดการคอมมาและแปลงเป็นตัวเลข (เพื่อให้คำนวณและแสดงผลไม่เพี้ยน)
                        for col in ['ถอนเงิน/ฝากเงิน', 'ยอดคงเหลือ']:
                            df[col] = df[col].astype(str).str.replace(',', '')
                            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
                
                        # 4. จัดการวันที่ (เติมค่าว่าง และเตรียมสำหรับการเรียงลำดับ)
                        df['วันที่'] = df['วันที่'].replace(r'^\s*$', pd.NA, regex=True).ffill()
                        
                        # สร้างคอลัมน์ชั่วคราวเพื่อใช้เรียงลำดับ (วันที่ + เวลา)
                        df['temp_sort'] = pd.to_datetime(df['วันที่'] + ' ' + df['เวลา'], dayfirst=True, errors='coerce')
                
                        # 5. *** เรียงลำดับจากเก่าไปใหม่ (Oldest to Newest) ***
                        # การเรียงแบบ ascending=True จะเอาวันที่ 02/06 ไว้บนสุด และ 26/06 ไว้ล่างสุด
                        # ทำให้ยอดคงเหลือไล่เรียงกันไปตามลำดับเวลาจริง
                        df = df.sort_values(by='temp_sort', ascending=True).reset_index(drop=True)
                        
                        # ลบคอลัมน์ชั่วคราวทิ้ง
                        df = df.drop(columns=['temp_sort'])
                
                        # 6. แปลงวันที่เป็นรูปแบบ dd/mm/yyyy เพื่อความสวยงามใน Excel
                        df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True).dt.strftime('%d/%m/%Y')
                        df['วันที่มีผล'] = pd.to_datetime(df['วันที่มีผล'], dayfirst=True).dt.strftime('%d/%m/%Y')                               
                        
                # --- 2. กลุ่มธนาคารอื่นๆ (Rule-based) ห้ามยุ่งส่วนประมวลผลเดิม ---
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
                        
                        elif bank_option == "ยูโอบี (UOB)":
                            raw_uob = parse_uob_pdf(unlocked_io)
                            uob_data = [[
                                r["st_date"], r["val_date"], r["tx_date"], 
                                r["tx_time"], clean_description(r["desc"]), 
                                (r["deposit"] - r["withdrawal"]), r["balance"]
                            ] for r in raw_uob]
                            df = pd.DataFrame(uob_data, columns=["Statement Date", "Value Date", "Transaction Date", "Transaction Time", "Description", "Deposit/Withdrawal", "Balance"])
                            df['Statement Date'] = pd.to_datetime(df['Statement Date'], format='%d/%m/%Y', errors='coerce')
                            df['Value Date'] = pd.to_datetime(df['Value Date'], format='%d/%m/%Y', errors='coerce')
                            df['Transaction Date'] = pd.to_datetime(df['Transaction Date'], format='%d/%m/%Y', errors='coerce')

                # ตรวจสอบว่า df ถูกสร้างสำเร็จหรือไม่ก่อน append
                if df is not None:
                    all_dfs.append(df)

            if all_dfs:
                final_df = pd.concat(all_dfs, ignore_index=True)
                st.dataframe(final_df, use_container_width=True)

                # --- ส่วน Export Excel (โค้ดส่วนนี้เหมือนเดิมทุกประการ) ---
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='m/d/yyyy') as writer:
                    final_df.to_excel(writer, index=False, sheet_name='Statement')
                    workbook = writer.book
                    worksheet = writer.sheets['Statement']
                    
                    colors = {"กสิกรไทย (KBank)": '#00A950', "ไทยพาณิชย์ (SCB)": '#4E2E7F', "กรุงไทย (KTB)": '#00A1E0', "กรุงศรี (BAY)": '#FFCC00', "กรุงเทพ (BBL)": '#0A22A8', "ยูโอบี (UOB)": '#003399'}
                    h_color = colors.get(bank_option, '#333333')
                    f_color = 'black' if bank_option == "กรุงศรี (BAY)" else 'white'
                    
                    header_fmt = workbook.add_format({'bold': True, 'bg_color': h_color, 'font_color': f_color, 'align': 'center', 'border': 1})
                    num_fmt = workbook.add_format({'num_format': '_(* #,##0.00_);_(* (#,##0.00);_(* "-"??_);_(@_)', 'align': 'right', 'valign': 'vcenter'})
                    date_fmt = workbook.add_format({'num_format': 'm/d/yyyy', 'align': 'left'})
                    
                    for col_num, value in enumerate(final_df.columns.values):
                        worksheet.write(0, col_num, value, header_fmt)
                    worksheet.set_column('A:Z', 18)
                    for idx, col_name in enumerate(final_df.columns):
                        if "Date" in col_name or "วันที่" in col_name: worksheet.set_column(idx, idx, 15, date_fmt)
                        if any(kw in col_name for kw in ["ถอนเงิน", "ฝากเงิน", "ยอดคงเหลือ", "จำนวนเงิน", "ภาษี", "Deposit/Withdrawal", "Balance"]):
                            worksheet.set_column(idx, idx, 15, num_fmt)

                output.seek(0)
                st.download_button(label="📥 ดาวน์โหลดไฟล์ Excel", data=output, file_name=f"Statement_{bank_option}_{datetime.now().strftime('%Y%m%d')}.xlsx")
                status_placeholder.success("✅ แปลงไฟล์สำเร็จ!")

        except PasswordError:
            st.error("❌ รหัสผ่านไม่ถูกต้อง")
        except Exception as e:
            st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
