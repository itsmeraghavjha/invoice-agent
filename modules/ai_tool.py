



# import os
# import time
# import json
# from datetime import date
# import google.generativeai as genai
# from google.api_core.exceptions import ResourceExhausted
# from tenacity import retry, retry_if_exception_type, wait_fixed


# class InvoiceExtractor:
#     def __init__(self, api_key):
#         self.model = genai.GenerativeModel('gemini-2.5-flash')

#     @retry(retry=retry_if_exception_type(ResourceExhausted), wait=wait_fixed(30))
#     def _generate(self, file_path, prompt):
#         if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
#             print(f"⚠️ Skipping upload: File {file_path} is empty or corrupted.")
#             return {}

#         g_file = None
#         for attempt in range(3):
#             try:
#                 g_file = genai.upload_file(file_path)
#                 break
#             except Exception as e:
#                 if attempt == 2:
#                     raise Exception(f"Gemini API rejected the file upload: {e}")
#                 time.sleep(2)

#         while g_file.state.name == "PROCESSING":
#             time.sleep(1)
#             g_file = genai.get_file(g_file.name)

#         if g_file.state.name == "FAILED":
#             raise Exception("Gemini processing failed")

#         try:
#             result = self.model.generate_content([prompt, g_file])
#             text = result.text.strip()
#             if text.startswith("```json"):
#                 text = text[7:]
#             elif text.startswith("```"):
#                 text = text[3:]
#             if text.endswith("```"):
#                 text = text[:-3]
#             return json.loads(text.strip())
#         finally:
#             if g_file:
#                 try:
#                     genai.delete_file(g_file.name)
#                 except:
#                     pass

#     def _first_of_current_month(self) -> str:
#         """Returns the 1st of the current month in DD.MM.YYYY format."""
#         today = date.today()
#         return f"01.{today.month:02d}.{today.year}"

#     def process_file(self, file_path):
#         fallback_date = self._first_of_current_month()  # e.g. "01.05.2026"

#         prompt = f"""
#         Analyze this invoice and return a valid JSON object.

#         CRITICAL RULES FOR 'invoice_number':
#         1. Only extract from PRINTED text inside a clearly labeled field such as 
#         "Invoice No", "Invoice No.", "Bill No", "Tax Invoice Number", or "Document No".
#         2. IGNORE anything handwritten, stamped, or annotated on the document — 
#         regardless of where it appears (top, corners, margins, body). These are 
#         internal notes added by the recipient, not the vendor's invoice number.
#         3. The correct invoice number is always printed by the vendor inside a 
#         structured label/table/box on the document.
#         4. DO NOT extract GSTIN (15 chars), PAN (10 chars), PO numbers, GRN numbers, 
#         or any stamp/seal text.
#         5. May contain slashes (/), hyphens (-), or financial year indicators (e.g. 25-26).

#         CRITICAL RULES FOR 'po_numbers':
#         1. SAP PO numbers must be exactly 10 digits, in range 3300000000 to 8259999999.
#         2. Look ONLY at PRINTED/TYPED text in structured labeled fields such as 
#         "Buyer's Order No.", "PO Number", "Reference No.", or "Our Order No."
#         3. STRICTLY IGNORE any number that is handwritten, stamped, annotated, 
#         or added in pen/pencil anywhere on the document — even if it falls within 
#         the valid 10-digit range. Handwritten additions are internal recipient notes, 
#         never vendor-issued PO references.
#         4. A valid PO is ONLY one that is machine-printed by the vendor inside a 
#         structured field or table cell.
#         5. DO NOT extract Sales Orders (prefixed with SO/DISSO) or any number outside 
#         the valid range or not exactly 10 digits.
#         6. If no valid printed PO found, return empty array [].

#         CRITICAL RULES FOR 'invoice_date':
#         1. Return strictly in DD.MM.YYYY format. Correct obvious OCR errors.
#         2. If the invoice date is missing, illegible, or cannot be determined, 
#         return "{fallback_date}" (the 1st of the current month).

#         CRITICAL RULES FOR 'first_line_item_description':
#         1. Extract ONLY from the first numbered row (SI.No / Line / Sr.No = 1) in the 
#         line items table.
#         2. IGNORE any bold or plain text labels, category headers, or column group titles 
#         that appear above the numbered rows inside the description column — these are 
#         structural labels, not line items.
#         3. A description cell may span multiple lines — concatenate all lines belonging 
#         to that cell into a single space-separated string.
#         4. EXCLUDE any lines within the cell that are metadata key-value pairs, 
#         identifiable by a label followed by a colon (e.g. "Lot Number:", 
#         "Net Unit Wt:", "Batch No:", "Expiry Date:").
#         5. Return only the core product name and code. Max 150 characters.

#         Required JSON Structure:
#         {{
#             "invoice_number": "string",
#             "invoice_date": "string (DD.MM.YYYY — use {fallback_date} if missing)",
#             "po_numbers": ["string"],
#             "vendor_name": "string",
#             "first_line_item_description": "string",
#             "net_amount": number,
#             "gross_amount": number
#         }}
#         """
#         return self._generate(file_path, prompt)

#     def process_non_po_file(self, file_path):
#         fallback_date = self._first_of_current_month()

#         prompt = f"""
#         Analyze this Non-PO invoice and return a valid JSON object.

#         CRITICAL RULE FOR 'withholding_tax_base' (TDS):
#         1. Look for explicit TDS deductions ("Less TDS"). 
#         2. If TDS is NOT explicitly printed on the document (like in freight bills), 
#         extract the TAXABLE VALUE (the total amount before GST is applied) as the 
#         withholding_tax_base.

#         CRITICAL RULE FOR 'invoice_number':
#         1. Do NOT extract GSTIN, PAN, or SAC codes as the invoice number.
#         2. Look for "Bill No", "Invoice No", or "L.R. No".

#         CRITICAL RULES FOR 'invoice_date':
#         1. Return strictly in DD.MM.YYYY format. Correct obvious OCR errors.
#         2. If the invoice date is missing, illegible, or cannot be determined, 
#         return "{fallback_date}" (the 1st of the current month).

#         Required JSON Structure:
#         {{
#             "invoice_number": "string",
#             "invoice_date": "string (DD.MM.YYYY — use {fallback_date} if missing)",
#             "header_text": "string",
#             "withholding_tax_base": number,
#             "gross_amount": number,
#             "line_items": [
#                 {{
#                     "item_text": "string",
#                     "item_gross_value": number,
#                     "hsn_code": "string"
#                 }}
#             ]
#         }}
#         """
#         return self._generate(file_path, prompt)







import os
import json
from datetime import date
from google import genai
from google.genai import types
from google.api_core.exceptions import ResourceExhausted
from tenacity import retry, retry_if_exception_type, wait_fixed


class InvoiceExtractor:
    def __init__(self, api_key):
        self.client = genai.Client(api_key=api_key)

    @retry(retry=retry_if_exception_type(ResourceExhausted), wait=wait_fixed(30))
    def _generate(self, file_path, prompt):
        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            print(f"⚠️ Skipping upload: File {file_path} is empty or corrupted.")
            return {}

        uploaded_file = None
        for attempt in range(3):
            try:
                uploaded_file = self.client.files.upload(file=file_path)
                break
            except Exception as e:
                if attempt == 2:
                    raise Exception(f"Gemini API rejected the file upload: {e}")

        try:
            response = self.client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[uploaded_file, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0
                )
            )
            return json.loads(response.text)
        finally:
            if uploaded_file:
                try:
                    self.client.files.delete(name=uploaded_file.name)
                except Exception:
                    pass

    def _first_of_current_month(self) -> str:
        """Returns the 1st of the current month in DD.MM.YYYY format."""
        today = date.today()
        return f"01.{today.month:02d}.{today.year}"

    def process_file(self, file_path):
        fallback_date = self._first_of_current_month()

        prompt = f"""
        Analyze this invoice and return a valid JSON object.

        CRITICAL RULES FOR 'invoice_number':
        1. Only extract from PRINTED text inside a clearly labeled field such as 
        "Invoice No", "Invoice No.", "Bill No", "Tax Invoice Number", or "Document No".
        2. IGNORE anything handwritten, stamped, or annotated on the document — 
        regardless of where it appears (top, corners, margins, body). These are 
        internal notes added by the recipient, not the vendor's invoice number.
        3. The correct invoice number is always printed by the vendor inside a 
        structured label/table/box on the document.
        4. DO NOT extract GSTIN (15 chars), PAN (10 chars), PO numbers, GRN numbers, 
        or any stamp/seal text.
        5. May contain slashes (/), hyphens (-), or financial year indicators (e.g. 25-26).
        6. Priority order for invoice number extraction:
            a. "Invoice No" / "Bill No" / "Tax Invoice Number" / "Document No"
            b. "GST E-Invoice No" (always prefer this over SAP internal numbers)
            c. "SAP Document No" ONLY if no GST E-Invoice No exists
        7. NEVER confuse "SAP Document No" with the invoice number when 
        "GST E-Invoice No" is present on the same document.
        8. For Diversey invoices specifically, the correct invoice number is 
        always the "GST E-Invoice No" field (format: MI followed by digits).
        9. Pay close attention to ambiguous characters — do NOT default to digits:
            - Leading 'I' (capital i) must NOT be converted to '1' (one)
            - Leading 'O' (capital o) must NOT be converted to '0' (zero)
            - 'U' must NOT be converted to '0'
            - 'Z' must NOT be converted to '2'
        Preserve the exact character as printed, even if it looks like a digit.
        10. If the invoice number begins with a single letter followed by digits 
            (e.g. I0419, O2345), treat the first character as a LETTER, not a number.

        CRITICAL RULES FOR 'po_numbers':
        1. SAP PO numbers must be exactly 10 digits, in range 3300000000 to 8259999999.
        2. Look ONLY at PRINTED/TYPED text in structured labeled fields such as 
        "Buyer's Order No.", "PO Number", "Reference No.", or "Our Order No."
        3. STRICTLY IGNORE any number that is handwritten, stamped, annotated, 
        or added in pen/pencil anywhere on the document — even if it falls within 
        the valid 10-digit range. Handwritten additions are internal recipient notes, 
        never vendor-issued PO references.
        4. A valid PO is ONLY one that is machine-printed by the vendor inside a 
        structured field or table cell.
        5. DO NOT extract Sales Orders (prefixed with SO/DISSO) or any number outside 
        the valid range or not exactly 10 digits.
        6. If no valid printed PO found, return empty array [].
        7. IGNORE any large handwritten number that appears as a standalone annotation 
        between header fields or in blank spaces — even if it is 10 digits and in 
        the valid range. These are internal recipient stamps (GRN/document tracking).
        8. A valid PO number always appears immediately next to its label on the SAME 
        line or in the SAME table cell (e.g., "P.O.No. : 3300014454"). 
        Any number floating in whitespace without a label is NOT a PO number.
        9. If multiple PO numbers are found, return them sorted in ascending 
        numeric order (smallest number first).
        Example: ["3300014454", "4100248206", "4500123456"]

        CRITICAL RULES FOR 'invoice_date':
        1. Return strictly in DD.MM.YYYY format. Correct obvious OCR errors.
        2. If the invoice date is missing, illegible, or cannot be determined, 
        return "{fallback_date}" (the 1st of the current month).

        CRITICAL RULES FOR 'first_line_item_description':
        1. Extract ONLY from the first numbered row (SI.No / Line / Sr.No = 1) in the 
        line items table.
        2. IGNORE any bold or plain text labels, category headers, or column group titles 
        that appear above the numbered rows inside the description column — these are 
        structural labels, not line items.
        3. A description cell may span multiple lines — concatenate all lines belonging 
        to that cell into a single space-separated string.
        4. EXCLUDE any lines within the cell that are metadata key-value pairs, 
        identifiable by a label followed by a colon (e.g. "Lot Number:", 
        "Net Unit Wt:", "Batch No:", "Expiry Date:").
        5. Return only the core product name and code. Max 150 characters.

        Required JSON Structure:
        {{
            "invoice_number": "string",
            "invoice_date": "string (DD.MM.YYYY — use {fallback_date} if missing)",
            "po_numbers": ["string"],
            "vendor_name": "string",
            "first_line_item_description": "string",
            "net_amount": number,
            "gross_amount": number
        }}
        """

        # ↓ CHANGED: store result, sort POs, then return
        result = self._generate(file_path, prompt)
        if result and "po_numbers" in result:
            result["po_numbers"] = sorted(
                result["po_numbers"],
                key=lambda x: int(x) if x.isdigit() else x
            )
        return result



    def process_non_po_file(self, file_path):
        fallback_date = self._first_of_current_month()

        prompt = f"""
        Analyze this Non-PO invoice and return a valid JSON object.

        CRITICAL RULE FOR 'withholding_tax_base' (TDS):
        1. Look for explicit TDS deductions ("Less TDS"). 
        2. If TDS is NOT explicitly printed on the document (like in freight bills), 
        extract the TAXABLE VALUE (the total amount before GST is applied) as the 
        withholding_tax_base.

        CRITICAL RULE FOR 'invoice_number':
        1. Do NOT extract GSTIN, PAN, or SAC codes as the invoice number.
        2. Look for "Bill No", "Invoice No", or "L.R. No".

        CRITICAL RULES FOR 'invoice_date':
        1. Return strictly in DD.MM.YYYY format. Correct obvious OCR errors.
        2. If the invoice date is missing, illegible, or cannot be determined, 
        return "{fallback_date}" (the 1st of the current month).

        Required JSON Structure:
        {{
            "invoice_number": "string",
            "invoice_date": "string (DD.MM.YYYY — use {fallback_date} if missing)",
            "header_text": "string",
            "withholding_tax_base": number,
            "gross_amount": number,
            "line_items": [
                {{
                    "item_text": "string",
                    "item_gross_value": number,
                    "hsn_code": "string"
                }}
            ]
        }}
        """
        return self._generate(file_path, prompt)