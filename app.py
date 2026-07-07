import io
import re
import json
import pandas as pd
import pikepdf
import pdfplumber
import streamlit as st
from pikepdf import PasswordError
from datetime import datetime
# --- เพิ่มการ Import สำหรับ Gemini ---
from google import genai
from google.genai import types
import os

# ตั้งค่าหน้าเว็บ Streamlit
st.set_page_config(page_title="PDF Statement Converter", layout="wide")

# ================= 0. AI Configuration (สำหรับ BAY) =================
# แนะนำให้ใช้ st.secrets หรือใส่ใน Sidebar เพื่อความปลอดภัย
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]

def process_bay_with_gemini(file_bytes, password):
    """ฟังก์ชันจัดการไฟล์ BAY ด้วย Gemini AI"""
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    # ปลดล็อค PDF ก่อนส่งให้ AI
    unlocked_bytes = file_bytes
    try:
        with pikepdf.open(io.BytesIO(file_bytes), password=password) as pdf:
            out_pdf = io.BytesIO()
            pdf.save(out_pdf)
            unlocked_bytes = out_pdf.getvalue()
    except:
        pass # ถ้าไม่มีรหัสผ่านหรือผ่านไปแล้วให้ใช้ bytes เดิม

    model_name = "gemini-2.5-flash" # ปรับจาก 2.5 เป็น 1.5 เพื่อความถูกต้องของเวอร์ชันปัจจุบัน
    
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
        # ลบ Markdown ถ้ามี
        if res_text.startswith("```"):
            res_text = res_text.replace("```json", "").replace("```", "").strip()
        return json.loads(res_text)
    except Exception as e:
        st.error(f"Gemini Error: {str(e)}")
        return None

# ================= 1. ฟังก์ชันช่วยเหลือ (Common Helpers) =================
def str_to_float(val_str):
    if val_str in [None, "", "-", " "]: return None
    try:
        clean_val = re.sub(r'[^\d.-]', '', str(val_str))
        return float(clean_val)
    except:
        return None

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
    found_channel, detail_part = "-", text
    for c in channels:
        if c in text:
            found_channel = c
            detail_part = text.replace(c, "").strip()
            break
    return found_channel, detail_part

# ================= 2. Logic สำหรับ KBank / SCB / KTB (คงเดิม) =================
# ===== 1.KBank =====
def parse_kbank_pdf(pdf_stream):
    all_parsed_rows = []
    bf_keywords = ["ยอดยกมา", "Balance Brought Forward", "Brought Forward"]
    # ปรับ table_headers ให้เป็นคำที่เฉพาะเจาะจงขึ้น เพื่อไม่ให้ชนกับรายการ "ถอนเงินสด"
    table_headers = ["เวลา/", "วันที่มีผล", "ถอนเงิน / ฝากเงิน", "ยอดคงเหลือ","ทำรายการ (บาท)"]

    with pdfplumber.open(pdf_stream) as pdf_obj:
        for page in pdf_obj.pages:
            text = page.extract_text()
            if not text: continue
            lines = text.split('\n')
            is_in_table = False 

            for line in lines:
                line = line.strip()
                if not line: continue
                
                # --- 1. เช็ค Pattern วันที่ก่อน (ถ้าเจอวันที่ แสดงว่าเป็นข้อมูลธุรกรรมแน่นอน ไม่ใช่หัวตาราง) ---
                date_match = re.match(r'^(\d{2}-\d{2}-\d{2})', line)
                
                if date_match:
                    is_in_table = True 
                    date = date_match.group(1)
                    time_match = re.search(r'(\d{2}:\d{2})', line)
                    time = time_match.group(1) if time_match else ""
                    
                    # หาตัวเลขจำนวนเงินทั้งหมดในบรรทัด
                    amounts = re.findall(r'[\d,]+\.\d{2}', line)
                    
                    temp_text = line.replace(date, "", 1).strip()
                    if time: temp_text = temp_text.replace(time, "", 1).strip()
                    
                    # แยก Description: ตัดข้อความก่อนเจอตัวเลขชุดแรก
                    desc = temp_text.split(amounts[0])[0].strip() if amounts else temp_text
                    
                    amount_val, balance = None, None
                    if len(amounts) == 1:
                        balance = str_to_float(amounts[0])
                    elif len(amounts) >= 2:
                        # แยกฝั่งเงินเข้า/ออก: เพิ่ม Keyword ให้ครอบคลุม
                        is_deposit = any(kw in desc for kw in ["รับเงิน", "คืนเงิน", "ฝาก", "เงินคืน", "Thai QR", "รับโอนเงิน"])
                        val = str_to_float(amounts[0])
                        amount_val = val if is_deposit else -val
                        balance = str_to_float(amounts[-1])

                    remaining = ""
                    if amounts:
                        # หาข้อความส่วนที่เหลือหลังยอดคงเหลือ
                        parts = line.split(amounts[-1])
                        if len(parts) > 1: remaining = parts[-1].strip()
                    
                    chan, det = split_channel_and_detail(remaining)
                    all_parsed_rows.append([date, time, desc, amount_val, balance, chan, det])
                    continue # เมื่อเจอข้อมูลแล้ว ให้ข้ามไปบรรทัดถัดไปทันที (ไม่ลงไปเช็ค Header ด้านล่าง)

                # --- 2. เช็คว่าเป็นหัวตารางหรือไม่ (ถ้าไม่มีวันที่) ---
                if any(kw in line for kw in table_headers):
                    is_in_table = True
                    continue
                
                # --- 3. เช็คบรรทัดจบรายการ ---
                if any(kw in line for kw in ["Total", "รวมทั้งสิ้น", "จบรายการ"]):
                    is_in_table = False
                    continue

                # --- 4. บรรทัดรายละเอียดเพิ่มเติม (ไม่มีวันที่ แต่อยู่ในตาราง) ---
                if is_in_table:
                    if any(x in line for x in ["หน้า", "แผ่นที่", "ยอดคงเหลือ"]): continue
                    c_extra, d_extra = split_channel_and_detail(line)
                    all_parsed_rows.append(["", "", "", None, None, c_extra if c_extra != "-" else "", d_extra])

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

# ===== 2.SCB =====
def parse_scb_pdf(pdf_stream):
    all_parsed_rows = []
    bf_keywords = ["ยอดยกมา", "ยอดเงินคงเหลือยกมา", "BALANCE BROUGHT FORWARD"]
    ignore_keywords = [
        "Date/Time", "Code", "Channel", "Cheque No.", "Withdrawal", "Deposit", "Description",
        "Balance Carried Forward", "Total Credit Amount", "Total Debit Amount",
        "จำนวนเงินนำเข้าบัญชีทั้งหมด", "จำนวนเงินที่หักบัญชีทั้งหมด",
        "เอกสารนี้ไม่จำเป็นต้องมีลายเซ็น", "จัดพิมพ์ผ่านระบบคอมพิวเตอร์",
        "สอบถามข้อมูลเพิ่มเติม", "02-722-2222", "Contact Center", "หน้าที่ (Page)", 
        "ช่องทาง", "เลขที่เช็ค", "ยอดเงินหักบัญชี", "ยอดเงินเข้าบัญชี", "รายการ (Items)",
        "ลูกหนี้/เจ้าหนี้", "ยอดเงินคงเหลือ", "TOTAL AMOUNT", "เอกสารฉบับนี้", "TOTAL ITEMS", "This document"
    ]
    pending_desc = ""
    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text: continue
            lines = text.split('\n')
            for line in lines:
                line = line.strip()
                if not line: continue
                if any(kw in line.upper() for kw in bf_keywords):
                    amounts = re.findall(r'[\d,]+\.\d{2}', line)
                    if amounts:
                        balance = str_to_float(amounts[-1])
                        all_parsed_rows.append([None, None, "B/F", "-", 0.0, balance, "ยอดยกมา (BALANCE BROUGHT FORWARD)"])
                    continue
                if any(kw in line for kw in ignore_keywords): continue
                transaction_match = re.match(r'^(\d{2}/\d{2}/\d{2,4})\s+(\d{2}:\d{2})', line)
                if transaction_match:
                    date_str = transaction_match.group(1)
                    time_str = transaction_match.group(2)
                    amounts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', line)
                    temp_text = line.replace(date_str, "").replace(time_str, "").strip()
                    parts = temp_text.split()
                    code = parts[0] if len(parts) > 0 else "-"
                    channel = parts[1] if len(parts) > 1 and not re.match(r'[\d,]+\.\d{2}', parts[1]) else "-"
                    amount_val, balance_val = 0.0, 0.0
                    if len(amounts) >= 2:
                        balance_val = str_to_float(amounts[-1])
                        raw_amount = str_to_float(amounts[-2])
                        if code.upper() in ['X1', 'IN', 'IT', 'BT', 'DP', 'CR', 'C1', 'NR']:
                            amount_val = raw_amount
                        else:
                            amount_val = -raw_amount
                    elif len(amounts) == 1:
                        balance_val = str_to_float(amounts[0])
                    line_desc = line.replace(date_str, "").replace(time_str, "").replace(code, "", 1)
                    if channel != "-": line_desc = line_desc.replace(channel, "", 1)
                    for amt in amounts: line_desc = line_desc.replace(amt, "")
                    final_desc = (pending_desc + " " + line_desc.strip()).strip()
                    pending_desc = ""
                    all_parsed_rows.append([date_str, time_str, code, channel, amount_val, balance_val, final_desc])
                elif all_parsed_rows:
                    if line.startswith(("รับโอนจาก", "โอนไป", "รับเงินโอน", "ชำระเงิน", "จากระบบ", "ค่าธรรมเนียม")):
                        pending_desc = (pending_desc + " " + line).strip()
                    else:
                        all_parsed_rows[-1][6] = (all_parsed_rows[-1][6] + " " + line).strip()
    return all_parsed_rows

# ===== 3.KTB =====
def parse_ktb_pdf(pdf_stream):
    all_raw_rows = []
    # รายการที่เป็น "เงินเข้า" (สำหรับรูปแบบ Personal)
    deposit_codes = ['IORSDT', 'IIPS', 'DDSDT', 'CR', 'OTHDEP']
    # คำหลักสำหรับยอดยกมา
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

    tax_keywords = ["ภาษี", "TAX", "WHT", "หักภาษี", "IIPS"]

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
                if any(kw in line for kw in ignore_keywords):
                    if not re.search(r'\d+\.\d{2}', line):
                        continue

                # --- 1. ตรวจสอบ "ยอดยกมา" (เพิ่มช่องภาษีเป็น 0.0) ---
                if any(kw in line for kw in bf_keywords):
                    amts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', line)
                    date_match = re.search(r'(\d{2}/\d{2}/\d{2,4})', line)
                    d_val = date_match.group(1) if date_match else ""
                    
                    if amts:
                        balance_val = str_to_float(amts[-1])
                        # โครงสร้าง: [วันที่, เวลา, รายการ, รายละเอียด, ถอนเงิน/ฝากเงิน, ภาษี, ยอดคงเหลือ, สาขา]
                        all_raw_rows.append([d_val, "", "B/F", "ยอดยกมา (Balance Brought Forward)", 0.0, 0.0, balance_val, "-"])
                        last_idx = len(all_raw_rows) - 1
                        continue

                # --- 2. รูปแบบ Biz Format (YYYY) ---
                biz_match = re.match(r'^(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})\s+(\w+)\s+(.*)', line)
                if biz_match:
                    d, t, c, rem = biz_match.groups()
                    amts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', rem)
                    if len(amts) >= 2:
                        val = str_to_float(amts[0])
                        balance_val = str_to_float(amts[-1])
                        detail = rem.split(amts[0])[0].strip()
                        branch = rem.split(amts[-1])[-1].strip() or "Krungthai Business"
                        
                        # แยกภาษี
                        is_tax = any(kw in (c + detail).upper() for kw in tax_keywords)
                        if is_tax:
                            f_amt = 0.0
                            tax_amt = -val # ภาษีมักเป็นยอดจ่าย/หักออก
                        else:
                            f_amt = val if ('DT' in c or 'CR' in c) else -val
                            tax_amt = 0.0
                        
                        all_raw_rows.append([d, t, c, detail, f_amt, tax_amt, balance_val, branch])
                        last_idx = len(all_raw_rows) - 1
                    continue

                # --- 3. รูปแบบ Personal Format (YY) ---
                main_match = re.match(r'^(\d{2}/\d{2}/\d{2})\s*(.*?)\s*\(([A-Z]+)\)\s*(.*)', line)
                if main_match:
                    d, name, c, rem = main_match.groups()
                    amts = re.findall(r'(\d{1,3}(?:,\d{3})*\.\d{2})', rem)
                    if len(amts) >= 2:
                        raw = str_to_float(amts[1]) if len(amts) >= 3 and str_to_float(amts[0]) == 0 else str_to_float(amts[0])
                        balance_val = str_to_float(amts[-1])
                        detail = rem.split(amts[0])[0].strip()
                        branch = line.split()[-1]

                        # แยกภาษี
                        is_tax = any(kw in (name + c + detail).upper() for kw in tax_keywords)
                        if is_tax:
                            f_amt = 0.0
                            tax_amt = -raw
                        else:
                            f_amt = raw if (c in deposit_codes or "เข้า" in name) else -raw
                            tax_amt = 0.0

                        all_raw_rows.append([d, "", f"{name} ({c})", detail, f_amt, tax_amt, balance_val, branch])
                        last_idx = len(all_raw_rows) - 1
                        continue

                # --- 4. รายละเอียดเพิ่มเติม (เพิ่มช่องภาษีเป็น None เพื่อให้ List มีความยาวเท่ากัน) ---
                time_row_match = re.match(r'^(\d{2}:\d{2})(.*)', line)
                if time_row_match and last_idx != -1:
                    all_raw_rows[last_idx][1] = time_row_match.group(1)
                    if time_row_match.group(2):
                        all_raw_rows[last_idx][3] += " " + time_row_match.group(2).strip()
                elif last_idx != -1:
                    if not re.match(r'^\d{2}/\d{2}/', line):
                        # เพิ่มค่าให้ครบ 8 คอลัมน์ (ใส่ None ในช่องเงินและภาษี)
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

# ================= 5. Streamlit UI & Export =================
st.title("📑 PDF Statement to Excel")

info_placeholder = st.empty()
info_placeholder.info("อัพโหลดไฟล์ PDF ระบบจะรวมข้อมูลเข้าด้วยกันตามลำดับ (รองรับ KBank, SCB, KTB และ BAY ด้วย AI)")

with st.sidebar:
    st.header("ตัวเลือก")
    bank_option = st.selectbox("เลือกธนาคาร", ["กสิกรไทย (KBank)", "ไทยพาณิชย์ (SCB)", "กรุงไทย (KTB)", "กรุงศรี (BAY)"])
    pdf_files = st.file_uploader("เลือกไฟล์ PDF", type="pdf", accept_multiple_files=True)
    password = st.text_input("รหัสผ่านไฟล์ (ถ้ามี)", type="password")
    convert_button = st.button("เริ่มการแปลงไฟล์")

if convert_button:
    if not pdf_files:
        st.error("⚠️ กรุณาเลือกไฟล์ PDF")
    else:
        status_placeholder = st.empty()
        progress_placeholder = st.empty()
        all_dfs = []
        
        try:
            for i, uploaded_file in enumerate(pdf_files):
                status_placeholder.write(f"⏳ กำลังประมวลผล: {uploaded_file.name}...")
                progress_placeholder.progress((i + 1) / len(pdf_files))
                
                pdf_bytes = uploaded_file.read()
                
                # --- แยกเงื่อนไขสำหรับ BAY (ใช้ AI) ---
                if bank_option == "กรุงศรี (BAY)":
                    data_rows = process_bay_with_gemini(pdf_bytes, password)
                    if data_rows:
                        header = ["วันที่", "เวลา", "ถอนเงิน/ฝากเงิน", "ยอดคงเหลือ", "รหัส", "รายละเอียด", "ช่องทาง", "รหัสสาขา"]
                        df = pd.DataFrame(data_rows, columns=header)
                        # แปลงวันที่จาก AI ให้เป็น Datetime (AI มักส่งมาเป็น dd/mm/yyyy หรือ yyyy-mm-dd)
                        df['วันที่'] = pd.to_datetime(df['วันที่'], dayfirst=True, errors='coerce')
                        df["ถอนเงิน/ฝากเงิน"] = pd.to_numeric(df["ถอนเงิน/ฝากเงิน"], errors='coerce')
                        df["ยอดคงเหลือ"] = pd.to_numeric(df["ยอดคงเหลือ"], errors='coerce')
                        all_dfs.append(df)
                
                # --- เงื่อนไขธนาคารอื่นๆ (ใช้ Rule-based) ---
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
                        all_dfs.append(df)

            if all_dfs:
                final_df = pd.concat(all_dfs, ignore_index=True)
                
                # ตรวจสอบว่าคอลัมน์ "วันที่" เป็น datetime หรือยัง (ถ้ามีแถวว่างให้ลบออกก่อนแสดงผล)
                st.dataframe(final_df, use_container_width=True)

                # Export Excel
                output = io.BytesIO()
                # กำหนด datetime_format ใน ExcelWriter เพื่อความชัวร์ชั้นแรก
                with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='m/d/yyyy') as writer:
                    final_df.to_excel(writer, index=False, sheet_name='Statement')
                    workbook = writer.book
                    worksheet = writer.sheets['Statement']

                    # สีตามธนาคาร
                    colors = {"กสิกรไทย (KBank)": '#00A950', "ไทยพาณิชย์ (SCB)": '#4E2E7F', "กรุงไทย (KTB)": '#00A1E0', "กรุงศรี (BAY)": '#FFCC00'}
                    h_color = colors.get(bank_option, '#333333')
                    f_color = 'black' if bank_option == "กรุงศรี (BAY)" else 'white'
                    
                    # สร้าง Format ต่างๆ
                    header_fmt = workbook.add_format({'bold': True, 'bg_color': h_color, 'font_color': f_color, 'align': 'center', 'border': 1})
                    num_fmt = workbook.add_format({'num_format': '_(* #,##0.00_);_(* (#,##0.00);_(* "-"??_);_(@_)', 'align': 'right', 'valign': 'vcenter'})
                    # Format พิเศษสำหรับวันที่ m/d/yyyy
                    date_fmt = workbook.add_format({'num_format': 'm/d/yyyy', 'align': 'left'})
                    
                    # เขียน Header พร้อมสี
                    for col_num, value in enumerate(final_df.columns.values):
                        worksheet.write(0, col_num, value, header_fmt)
                    
                    # ตั้งค่าความกว้างคอลัมน์ทั้งหมดเบื้องต้น
                    worksheet.set_column('A:Z', 18)
                    
                    # --- บังคับ Format วันที่ (คอลัมน์ A) ---
                    worksheet.set_column('A:A', 15, date_fmt)

                    # --- บังคับ Format ตัวเลข (ถอน/ฝาก และ ยอดคงเหลือ) ---
                    # หาตำแหน่งคอลัมน์ที่มีคำว่า "ถอน" หรือ "ยอดคงเหลือ"
                    for idx, col_name in enumerate(final_df.columns):
                        if any(kw in col_name for kw in ["ถอนเงิน", "ฝากเงิน", "ยอดคงเหลือ", "จำนวนเงิน"]):
                            worksheet.set_column(idx, idx, 15, num_fmt)

                output.seek(0)
                st.download_button(label="📥 ดาวน์โหลดไฟล์ Excel", data=output, 
                                 file_name=f"Statement_{bank_option}_{datetime.now().strftime('%Y%m%d')}.xlsx")
                status_placeholder.success("✅ แปลงไฟล์สำเร็จ!")

        except PasswordError:
            st.error("❌ รหัสผ่านไม่ถูกต้อง")
        except Exception as e:
            st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
