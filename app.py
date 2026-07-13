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
    1. คอลัมน์ 'จำนวนเงิน': หากเป็นการ 'ถอน' ให้ติดลบ (เช่น -4700.00) หากเป็น 'ฝาก' ให้เป็นบวก (เช่น 500.00) ห้ามมีตัวหนังสือ
    2. วันที่และเวลา: แยกออกจากกัน (เช่น "15/08/2025" และ "18:16:42")
    3. รายละเอียด: รวมข้อความคำอธิบายทั้งหมดให้อยู่ในบรรทัดเดียวกัน
    4. ห้ามมี Header ในข้อมูลที่ส่งกลับมา
    5. คืนค่าเฉพาะ JSON ห้ามมีคำอธิบายอื่น
    """

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=[
                types.Part.from_bytes(data=unlocked_bytes, mime_type="application/pdf"),
                prompt
            ],
            config=types.GenerateContentConfig(
                response_mime_type='application/json',
            ),
        )
        res_text = response.text.strip()
        if res_text.startswith("```"):
            res_text = res_text.replace("```json", "").replace("```", "").strip()
        return json.loads(res_text)
    except Exception as e:
        st.error(f"Gemini Error: {str(e)}")
        return None

# ================= 1. ฟังก์ชันช่วยเหลือ (Common Helpers) =================
def str_to_float(val_str):
    if val_str in [None, "", "-", " "]: return 0.0
    try:
        clean_val = re.sub(r'[^\d.-]', '', str(val_str))
        return float(clean_val)
    except:
        return 0.0

def decode_cid(text):
    if not text: return ""
    cid_map = {
        "(cid:344)": "0", "(cid:345)": "1", "(cid:346)": "2", "(cid:347)": "3", "(cid:348)": "4",
        "(cid:349)": "5", "(cid:350)": "6", "(cid:351)": "7", "(cid:352)": "8", "(cid:353)": "9",
    }
    for cid, val in cid_map.items():
        text = text.replace(cid, val)
    return text

def split_channel_and_detail(text):
    channels = [
        "EDC/K SHOP/MYQR", "โอนเข้า/หักบัญชีอัตโนมัติ", "K PLUS", "ตู้เติมเงิน / โมบาย แอปพลิ", 
        "Internet/Mobile KK", "K BIZ", "EDC", "โอนเข้าหักบัญชีอัตโนมัติ", "ATM", "CDM", 
        "BRANCH", "K-Cash Connect Plus" , "Internet/Mobile GSB", "Internet/Mobile SCB", 
        "Internet/Mobile KTB ", "Internet/Mobile TTB", "ตู้เติมเงิน / โมบาย แอปพลิชัน", "Internet/Mobile BAY", 
        "Internet/Mobile BBL","Internet/Mobile BAAC", "สาขาถนนศรีสุริยวงศ์", "สาขาเซ็นทรัล ขอนแก่น"
    ]
    found_chan = "-"
    detail = text.strip()
    for c in channels:
        if c in text:
            found_chan = c
            detail = text.replace(c, "").strip()
            detail = detail.lstrip('/ ').strip()
            break
    return found_chan, detail

# --- ฟังก์ชันเฉพาะสำหรับ UOB ---
def clean_description_uob(text):
    replacements = {
        "MISCCREDIT": "MISC CREDIT", "MISCDEBIT": "MISC DEBIT",
        "PAYMENTEO": "PAYMENT EO", "INVOICENO": "INVOICE NO",
        "INTERESTCREDIT": "INTEREST CREDIT", "WITHHOLDINGTAXDR": "WITHHOLDING TAX DR"
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r'\s+', ' ', text).strip()

def is_garbage_line_uob(line):
    garbage_keywords = [
        "Account Statement", "Movement Details - From:",
        "Statement", "Value Date", "Transaction", "Description",
        "Deposit", "Withdrawal", "Balance", "Date/Time", "Date Date/Time",
        "Total in Account Currency", "Note:",
        "-Balances and details reflected are indicative", "TotalinAccountCurrency"
    ]
    line_upper = line.upper()
    if any(kw.upper() in line_upper for kw in garbage_keywords): return True
    if re.match(r'^\d+\s?/\s?\d+$', line): return True
    if re.match(r'^\d{2}/\d{2}/\d{4}$', line): return True
    return False

# ================= 2. Logic การ Parse PDF แต่ละธนาคาร =================

# --- 1. KBank ---
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
                date_match = re.match(r'^(\d{2}-\d{2}-\d{2})', line)
                if date_match:
                    is_in_table = True 
                    date = date_match.group(1)
                    time_match = re.search(r'(\d{2}:\d{2})', line)
                    time = time_match.group(1) if time_match else ""
                    amounts = re.findall(r'-?[\d,]+\.\d{2}', line)
                    temp_text = line.replace(date, "", 1).strip()
                    if time: temp_text = temp_text.replace(time, "", 1).strip()
                    desc = temp_text.split(amounts[0])[0].strip() if amounts else temp_text
                    amount_val, balance = None, None
                    if len(amounts) == 1:
                        balance = str_to_float(amounts[0])
                    elif len(amounts) >= 2:
                        is_deposit = any(kw in desc for kw in ["รับเงิน", "คืนเงิน", "ฝาก", "เงินคืน", "Thai QR", "รับโอนเงิน", "รับโอน", "รับเงินจาก"])
                        val = str_to_float(amounts[0])
                        amount_val = val if is_deposit else -val
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
                    if any(x in line for x in ["หน้า", "แผ่นที่", "ยอดคงเหลือ", "รวมถอนเงิน", "รวมฝากเงิน"]): continue
                    c_extra, d_extra = split_channel_and_detail(line)
                    all_parsed_rows.append(["", "", "", None, None, c_extra if c_extra != "-" else "", d_extra])

    rows_to_delete = set()
    n = len(all_parsed_rows)
    bf_indices = [idx for idx, row in enumerate(all_parsed_rows) if any(kw in str(row[2]) for kw in bf_keywords)]
    if bf_indices:
        keep_idx = next((idx for idx in bf_indices if all_parsed_rows[idx][0]), bf_indices[0])
        for idx in bf_indices:
            if idx != keep_idx: rows_to_delete.add(idx)
    i = 0
    while i < n:
        if all_parsed_rows[i][0] == "" and all_parsed_rows[i][3] is None:
            start_block = i
            while i < n and all_parsed_rows[i][0] == "" and all_parsed_rows[i][3] is None: i += 1
            if (i - start_block) > 3:
                for k in range(start_block, i): rows_to_delete.add(k)
        else: i += 1
    return [row for idx, row in enumerate(all_parsed_rows) if idx not in rows_to_delete]

# --- 2. SCB ---
def parse_scb_pdf(pdf_stream):
    all_parsed_rows = []
    header_found = False
    pending_desc = ""
    bf_keywords = ["ยอดยกมา", "BALANCE BROUGHT FORWARD", "ยอดเงินคงเหลือยกมา"]
    ignore_keywords = ["Date", "Time", "Code", "Channel", "Cheque No.", "Withdrawal", "Deposit", "Description", "Debit/Credit", "Balance/Baht", "วันที่", "เวลา", "รายการ", "ช่องทาง", "ยอดเงินคงเหลือ", "This document is auto-generated", "signature is not required", "THE SIAM COMMERCIAL BANK", "บริษัท", "STATEMENT OF SAVING", "เลขที่บัญชี", "ที่อยู่", "TOTAL ITEMS", "TOTAL AMOUNT", "หน้าที่", "Page", "เอกสารฉบับนี้"]

    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text: continue
            for line in text.split('\n'):
                line_clean = line.strip()
                if not line_clean: continue
                if any(kw.upper() in line_clean.upper() for kw in bf_keywords):
                    amounts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', line_clean)
                    if amounts:
                        all_parsed_rows.append([None, None, "B/F", "-", 0.0, str_to_float(amounts[-1]), "ยอดยกมา (BALANCE BROUGHT FORWARD)"])
                    header_found = True
                    continue
                if ("Date" in line_clean and "Time" in line_clean) or ("วันที่" in line_clean and "เวลา" in line_clean):
                    header_found = True
                    continue 
                if not header_found or any(kw in line_clean for kw in ignore_keywords): continue
                transaction_match = re.match(r'^(\d{2}/\d{2}/\d{2,4})\s+(\d{2}:\d{2})', line_clean)
                if transaction_match:
                    date_str, time_str = transaction_match.groups()
                    amounts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', line_clean)
                    temp_text = line_clean.replace(date_str, "").replace(time_str, "").strip()
                    parts = temp_text.split()
                    code = parts[0] if len(parts) > 0 else "-"
                    channel = parts[1] if len(parts) > 1 and not re.match(r'[\d,]+\.\d{2}', parts[1]) else "-"
                    amount_val, balance_val = 0.0, 0.0
                    if len(amounts) >= 2:
                        balance_val = str_to_float(amounts[-1])
                        raw_amount = str_to_float(amounts[-2])
                        credit_codes = ['X1', 'IN', 'IT', 'BT', 'DP', 'CR', 'SD', 'C1', 'NR', 'TRN']
                        amount_val = raw_amount if code.upper() in credit_codes else -raw_amount
                    elif len(amounts) == 1: balance_val = str_to_float(amounts[0])
                    desc_raw = line_clean.replace(date_str, "").replace(time_str, "").replace(code, "", 1)
                    if channel != "-": desc_raw = desc_raw.replace(channel, "", 1)
                    for amt in amounts: desc_raw = desc_raw.replace(amt, "")
                    final_desc = (pending_desc + " " + desc_raw.strip()).strip()
                    pending_desc = "" 
                    all_parsed_rows.append([date_str, time_str, code, channel, amount_val, balance_val, final_desc])
                elif all_parsed_rows:
                    keywords_desc = ("รับโอนจาก", "โอนไป", "รับเงินโอน", "ชำระเงิน", "จากระบบ", "ค่าธรรมเนียม", "PromptPay", "TO ", "FROM ")
                    if line_clean.startswith(keywords_desc): pending_desc = (pending_desc + " " + line_clean).strip()
                    else: all_parsed_rows[-1][6] = (all_parsed_rows[-1][6] + " " + line_clean).strip()

    temp_list_bf = []
    found_first_bf = False
    for row in all_parsed_rows:
        if any(kw in str(row[2]) for kw in bf_keywords):
            if not found_first_bf: temp_list_bf.append(row); found_first_bf = True
        else: temp_list_bf.append(row)
    return temp_list_bf

# --- 3. KTB ---
def parse_ktb_pdf(pdf_stream):
    all_raw_rows = []
    deposit_codes = ['IORSDT', 'IIPS', 'DDSDT', 'CR', 'OTHDEP', 'PBSDT', 'NBSDT']
    bf_keywords = ["ยอดยกมา", "ยอดคงเหลือยกมา", "Balance Brought Forward", "Brought Forward"]
    ignore_keywords = ["ธนาคารกรุงไทย", "หน้า", "รายการเดินบัญชี", "ชื่อบัญชี", "ที่อยู่", "Statement", "รวมรายการ", "บริษัท"]

    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            text = decode_cid(page.extract_text())
            if not text: continue
            lines = text.split('\n')
            last_idx = -1
            for line in lines:
                line = line.strip()
                if not line or (any(kw in line for kw in ignore_keywords) and not re.search(r'\d+\.\d{2}', line)): continue
                if any(kw in line for kw in bf_keywords):
                    amts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', line)
                    date_match = re.search(r'(\d{2}/\d{2}/\d{2,4})', line)
                    if amts:
                        all_raw_rows.append([date_match.group(1) if date_match else "", "", "B/F", "ยอดยกมา", 0.0, 0.0, str_to_float(amts[-1]), "-"])
                        last_idx = len(all_raw_rows) - 1
                    continue
                biz_match = re.match(r'^(\d{2}/\d{2}/\d{4})\s*(\d{2}:\d{2})?\s*([A-Z0-9]+)\s+(.*)', line)
                if biz_match:
                    d, t, c, rem = biz_match.groups()
                    amts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', rem)
                    f_amt, tax_amt, balance_val = 0.0, 0.0, 0.0
                    if len(amts) >= 2:
                        val_raw = str_to_float(amts[0])
                        balance_val = str_to_float(amts[-1])
                        if len(amts) >= 3: tax_amt = -abs(str_to_float(amts[1]))
                        f_amt = val_raw if any(dc in c for dc in deposit_codes) else -val_raw
                    all_raw_rows.append([d, t or "", c, rem.split(amts[0])[0].strip() if amts else rem, f_amt, tax_amt, balance_val, rem.split(amts[-1])[-1].strip() if amts else ""])
                    last_idx = len(all_raw_rows) - 1
                    continue
                pers_match = re.match(r'^(\d{2}/\d{2}/\d{2})\s*(.*?)\s*\(([A-Z]+)\)\s*(.*)', line)
                if pers_match:
                    d, name, c, rem = pers_match.groups()
                    amts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', rem)
                    f_amt, balance_val = 0.0, 0.0
                    if len(amts) >= 2:
                        balance_val = str_to_float(amts[-1])
                        if len(amts) >= 3:
                            w_amt, d_amt = str_to_float(amts[0]), str_to_float(amts[1])
                            f_amt = d_amt if d_amt > 0 else -w_amt
                        else:
                            raw = str_to_float(amts[0])
                            f_amt = raw if (c in deposit_codes or "เข้า" in name) else -raw
                    all_raw_rows.append([d, "", f"{name} ({c})", rem.split(amts[0])[0].strip() if amts else rem, f_amt, 0.0, balance_val, "Personal"])
                    last_idx = len(all_raw_rows) - 1
                    continue
                time_row_match = re.match(r'^(\d{2}:\d{2})(.*)', line)
                if time_row_match and last_idx != -1:
                    all_raw_rows[last_idx][1] = time_row_match.group(1)
                    if time_row_match.group(2): all_raw_rows[last_idx][3] += " " + time_row_match.group(2).strip()
                elif last_idx != -1 and not re.match(r'^\d{2}/\d{2}/', line):
                    all_raw_rows.append(["", "", "", line, None, None, None, ""])
    
    temp_list_bf = []
    found_first_bf = False
    for row in all_raw_rows:
        if row[2] == "B/F":
            if not found_first_bf: temp_list_bf.append(row); found_first_bf = True
        else: temp_list_bf.append(row)
    
    final_filtered_rows = []
    for row in temp_list_bf:
        if row[4] is not None or row[2] == "B/F": final_filtered_rows.append(row)
        elif final_filtered_rows: final_filtered_rows[-1][3] = (str(final_filtered_rows[-1][3]) + " " + str(row[3])).strip()
    return final_filtered_rows

# --- 4. BBL ---
def clean_thai_bbl(text):
    if not text: return ""
    text = unicodedata.normalize('NFKC', text)
    corrections = {"เงนิ": "เงิน", "บญั": "บัญ", "อตั": "อัต", "โนมตัิ": "โนมัติ", "คา่": "ค่า", "สะสมทรัพรั": "สะสมทรัพย์"}
    for wrong, right in corrections.items(): text = text.replace(wrong, right)
    return re.sub(r'\s+', ' ', text).strip()

def thai_date_to_eng(thai_date_str):
    months = {"ม.ค.": "01", "ก.พ.": "02", "มี.ค.": "03", "เม.ย.": "04", "พ.ค.": "05", "มิ.ย.": "06", "ก.ค.": "07", "ส.ค.": "08", "ก.ย.": "09", "ต.ค.": "10", "พ.ย.": "11", "ธ.ค.": "12"}
    try:
        parts = thai_date_str.split()
        return f"{parts[0].zfill(2)}/{months.get(parts[1], '01')}/{int(parts[2])-543}"
    except: return None

def parse_bbl_pdf(pdf_stream):
    all_rows = []
    date_pattern = r'(\d{1,2}\s+(?:ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.)\s+\d{4})'
    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            lines = page.extract_text(x_tolerance=2, y_tolerance=2).split('\n')
            for i, line in enumerate(lines):
                line = line.strip()
                date_matches = re.findall(date_pattern, line)
                if not date_matches or any(k in line for k in ["สรุปยอด", "รายการเคลื่อนไหว"]): continue
                time_val = ""
                time_in_line = re.search(r'(\d{2}:\d{2})', line)
                if time_in_line: time_val = time_in_line.group(1)
                amounts = re.findall(r'[\d,]+\.\d{2}', line)
                if not amounts: continue
                channel = ""
                chan_match = re.search(r'\b(BR\d+|DR\d+|AUTO|TELE|M-BANKING|INTERNET)\b', line)
                if chan_match: channel = chan_match.group(1)
                full_desc = clean_thai_bbl(line)
                balance = str_to_float(amounts[-1])
                tx_amt = 0.0
                if len(amounts) >= 2:
                    val = str_to_float(amounts[-2])
                    tx_amt = val if any(word in full_desc for word in ["ฝาก", "เข้า", "รับโอน", "คืน"]) else -val
                all_rows.append([thai_date_to_eng(date_matches[0]), time_val, thai_date_to_eng(date_matches[1]) if len(date_matches)>1 else "", full_desc, "", tx_amt, balance, channel])
    return all_rows

# --- 5. UOB (ใหม่) ---
def str_to_float(val_str):
    if not val_str: return 0.0
    try:
        val = str(val_str).replace(',', '').strip()
        return float(val)
    except:
        return 0.0

def clean_description(text):
    """ปรับแก้คำสะกดและเว้นวรรคให้ถูกต้อง"""
    replacements = {
        "MISCCREDIT": "MISC CREDIT",
        "MISCDEBIT": "MISC DEBIT",
        "PAYMENTEO": "PAYMENT EO",
        "INVOICENO": "INVOICE NO",
        "INTERESTCREDIT": "INTEREST CREDIT",
        "WITHHOLDINGTAXDR": "WITHHOLDING TAX DR"
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def is_garbage_line(line):
    """กรองบรรทัดขยะ หัวตาราง และส่วนสรุปท้ายเอกสาร"""
    garbage_keywords = [
        "Account Statement", "Movement Details - From:",
        "Statement", "Value Date", "Transaction", "Description",
        "Deposit", "Withdrawal", "Balance", "Date/Time", "Date Date/Time",
        "Total in Account Currency", "Note:",
        "-Balances and details reflected are indicative", "TotalinAccountCurrency"
    ]
    line_upper = line.upper()
    if any(kw.upper() in line_upper for kw in garbage_keywords):
        return True
    if re.match(r'^\d+\s?/\s?\d+$', line): 
        return True
    if re.match(r'^\d{2}/\d{2}/\d{4}$', line): 
        return True
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
            lines = text.split('\n')
            
            for line in lines:
                line = line.strip()
                if not line or is_garbage_line(line):
                    continue

                match_dates = re.match(row_start_pattern, line)
                if match_dates:
                    if current_row: all_rows.append(current_row)
                    
                    amounts = re.findall(r'[\d,]+\.\d{2}', line)
                    current_row = {
                        "st_date": match_dates.group(1),
                        "val_date": match_dates.group(2),
                        "tx_date": match_dates.group(3),
                        "tx_time": "",
                        "desc": "",
                        "deposit": 0.0, "withdrawal": 0.0, "balance": 0.0
                    }
                    
                    if len(amounts) >= 3:
                        current_row["deposit"] = str_to_float(amounts[-3])
                        current_row["withdrawal"] = str_to_float(amounts[-2])
                        current_row["balance"] = str_to_float(amounts[-1])
                        
                        desc_part = line[33:].strip()
                        desc_part = desc_part.split(amounts[-3])[0].strip()
                        current_row["desc"] = desc_part

                elif current_row and re.search(time_pattern, line):
                    t_match = re.search(time_pattern, line)
                    current_row["tx_time"] = t_match.group(1)
                    extra_desc = line.replace(current_row["tx_time"], "").strip()
                    current_row["desc"] += " " + extra_desc

                elif current_row:
                    current_row["desc"] += " " + line

        if current_row: all_rows.append(current_row)
    return all_rows

# ================= 5. Streamlit UI & Export =================
st.title("📑 PDF Statement to Excel")

with st.sidebar:
    st.header("ตัวเลือก")
    bank_option = st.selectbox("เลือกธนาคาร", ["กสิกรไทย (KBank)", "ไทยพาณิชย์ (SCB)", "กรุงไทย (KTB)", "กรุงศรี (BAY)", "กรุงเทพ (BBL)", "ยูโอบี (UOB)"])
    pdf_files = st.file_uploader("เลือกไฟล์ PDF", type="pdf", accept_multiple_files=True)
    password = st.text_input("รหัสผ่านไฟล์ (ถ้ามี)", type="password")
    convert_button = st.button("เริ่มการแปลงไฟล์", use_container_width=True)

if convert_button:
    if not pdf_files: st.error("⚠️ กรุณาเลือกไฟล์ PDF")
    else:
        status_placeholder = st.empty()
        all_dfs = []
        try:
            for i, uploaded_file in enumerate(pdf_files):
                status_placeholder.write(f"⏳ กำลังประมวลผล: {uploaded_file.name}...")
                pdf_bytes = uploaded_file.read()
                
                if bank_option == "กรุงศรี (BAY)":
                    data = process_bay_with_gemini(pdf_bytes, password)
                    if data: all_dfs.append(pd.DataFrame(data, columns=["วันที่", "เวลา", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "รหัส", "รายละเอียด", "ช่องทาง", "รหัสสาขา"]))
                else:
                    with pikepdf.open(io.BytesIO(pdf_bytes), password=password) as pdf:
                        unlocked_io = io.BytesIO()
                        pdf.save(unlocked_io); unlocked_io.seek(0)
                        
                        if bank_option == "กสิกรไทย (KBank)":
                            df = pd.DataFrame(parse_kbank_pdf(unlocked_io), columns=["วันที่", "เวลา", "รายการ", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "ช่องทาง", "รายละเอียด"])
                            df['วันที่'] = pd.to_datetime(df['วันที่'], format='%d-%m-%y', errors='coerce')
                        elif bank_option == "ไทยพาณิชย์ (SCB)":
                            df = pd.DataFrame(parse_scb_pdf(unlocked_io), columns=["วันที่", "เวลา", "รายการ", "ช่องทาง", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "รายละเอียด"])
                            df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True, errors='coerce')
                        elif bank_option == "กรุงไทย (KTB)":
                            df = pd.DataFrame(parse_ktb_pdf(unlocked_io), columns=["วันที่", "เวลา", "รายการ", "รายละเอียด", "ถอนเงิน/ฝากเงิน", "ภาษี", "ยอดคงเหลือ", "สาขา"])
                            df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True, errors='coerce')
                        elif bank_option == "กรุงเทพ (BBL)":
                            rows = parse_bbl_pdf(unlocked_io)
                            rows.reverse()
                            df = pd.DataFrame(rows, columns=["วันที่", "เวลา", "วันที่ที่มีผล", "รายละเอียด", "เลขที่เช็ค", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "ช่องทาง"])
                            df['วันที่'] = pd.to_datetime(df['วันที่'], format='%d/%m/%Y', errors='coerce')
                        elif bank_option == "ยูโอบี (UOB)":
                            df = pd.DataFrame(parse_uob_pdf(unlocked_io), columns=["Statement Date", "Value Date", "Transaction Date", "Transaction Time", "Description", "Deposit/Withdrawal", "Balance"])
                            df['วันที่'] = pd.to_datetime(df['วันที่'], format='%d/%m/%Y', errors='coerce')
                        all_dfs.append(df)

            if all_dfs:
                final_df = pd.concat(all_dfs, ignore_index=True)
                st.dataframe(final_df, use_container_width=True)
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='m/d/yyyy') as writer:
                    final_df.to_excel(writer, index=False, sheet_name='Statement')
                    workbook, worksheet = writer.book, writer.sheets['Statement']
                    colors = {"กสิกรไทย (KBank)": '#00A950', "ไทยพาณิชย์ (SCB)": '#4E2E7F', "กรุงไทย (KTB)": '#00A1E0', "กรุงศรี (BAY)": '#FFCC00', "กรุงเทพ (BBL)": '#0A22A8', "ยูโอบี (UOB)": '#003399'}
                    h_color = colors.get(bank_option, '#333333')
                    header_fmt = workbook.add_format({'bold': True, 'bg_color': h_color, 'font_color': 'white' if bank_option != "กรุงศรี (BAY)" else 'black', 'align': 'center', 'border': 1})
                    num_fmt = workbook.add_format({'num_format': '_(* #,##0.00_);_(* (#,##0.00);_(* "-"??_);_(@_)', 'align': 'right'})
                    date_fmt = workbook.add_format({'num_format': 'm/d/yyyy', 'align': 'left'})
                    for col_num, value in enumerate(final_df.columns.values): worksheet.write(0, col_num, value, header_fmt)
                    worksheet.set_column('A:A', 15, date_fmt)
                    for idx, col in enumerate(final_df.columns):
                        if any(kw in col for kw in ["ถอนเงิน", "ยอดคงเหลือ", "ภาษี"]): worksheet.set_column(idx, idx, 15, num_fmt)
                        elif "รายละเอียด" in col: worksheet.set_column(idx, idx, 50)
                output.seek(0)
                st.download_button(label="📥 ดาวน์โหลดไฟล์ Excel", data=output, file_name=f"Statement_{bank_option}_{datetime.now().strftime('%Y%m%d')}.xlsx")
                status_placeholder.success("✅ แปลงไฟล์สำเร็จ!")
        except Exception as e: st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
