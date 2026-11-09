import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime

class EmailSender:
    def __init__(self, config):
        self.sender_email = config.GMAIL_USER
        self.password = config.GMAIL_PASS
        # Expecting a list of strings from config
        self.receivers = config.RECEIVER_EMAILS 
        
    def send_report(self, attachment_path, count):
        if not os.path.exists(attachment_path):
            print(f"⚠️ Email Error: Attachment {attachment_path} not found.")
            return

        msg = MIMEMultipart()
        msg['From'] = self.sender_email
        
        # Join the list into a single string for the header (e.g. "a@b.com, c@d.com")
        msg['To'] = ", ".join(self.receivers)
        
        msg['Subject'] = f"Invoice Bot Report - {datetime.now().strftime('%d-%b-%Y')}"

        body = f"Hello Team,\n\nThe Invoice Bot has successfully processed {count} new invoices.\nPlease find the detailed report attached.\n\nRegards,\nInvoice Automation Agent"
        msg.attach(MIMEText(body, 'plain'))

        try:
            with open(attachment_path, "rb") as f:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header(
                    'Content-Disposition', 
                    f"attachment; filename={os.path.basename(attachment_path)}"
                )
                msg.attach(part)
        except Exception as e:
            print(f"⚠️ Failed to attach file: {e}")
            return

        try:
            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(self.sender_email, self.password)
            
            # Send to the list of receivers
            server.sendmail(self.sender_email, self.receivers, msg.as_string())
            
            server.quit()
            print(f"📧 Email sent successfully to {len(self.receivers)} recipients.")
        except Exception as e:
            print(f"❌ Failed to send email: {e}")