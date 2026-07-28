import os
import re
from config_manager import get_gmail_folder
from dotenv import load_dotenv
from email.header import decode_header
from imapclient import IMAPClient

# Load credentials from environment variables.
load_dotenv()
EMAIL = os.getenv("GMAIL_USER")
PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

if not EMAIL or not PASSWORD:
    raise ValueError("GMAIL_USER and GMAIL_APP_PASSWORD environment variables must be set")


def decode_email_subject(subject_raw):
    """Decode MIME-encoded email subjects, including emojis and special characters."""
    if not subject_raw:
        return ""
        
    decoded_fragments = decode_header(subject_raw)
    subject = ""
    
    for fragment, charset in decoded_fragments:
        if isinstance(fragment, bytes):
            # Decode bytes with the provided charset, defaulting to UTF-8.
            charset = charset or 'utf-8'
            subject += fragment.decode(charset, errors='replace')
        else:
            # Append fragments that are already decoded strings.
            subject += fragment
            
    return subject


def parse_github_subject(subject):
    """Parse a GitHub release subject into repo, tag, and release type."""
    # Capture content after "Release" or "Pre-release" until the final separator.
    pattern = r"^\[([^\]]+)\]\s+(?:Pre-)?Release\s+(.+)(?:\s+-\s+.*)$"
    match = re.search(pattern, subject, re.IGNORECASE)
    
    if match:
        repo = match.group(1)
        tag = match.group(2)
        release_type = "Pre-release" if "Pre-release" in subject else "Release"
        return repo, tag, release_type
    
    # Fallback for subjects that do not include the trailing separator.
    pattern_simple = r"^\[([^\]]+)\]\s+(?:Pre-)?Release\s+(.+)$"
    match_simple = re.search(pattern_simple, subject, re.IGNORECASE)
    if match_simple:
        repo = match_simple.group(1)
        tag = match_simple.group(2)
        release_type = "Pre-release" if "Pre-release" in subject else "Release"
        return repo, tag, release_type
        
    return None, None, None


def get_pending_notifications(limit=None):
    """Fetch unread GitHub release notifications from Gmail.

    Return a list of dicts with keys: repo, tag, release_type, email_id.
    
    Args:
        limit: Maximum number of emails to process. None means all.
    """
    # Validate required email credentials.
    if not EMAIL or not PASSWORD:
        raise ValueError("EMAIL and PASSWORD environment variables must be set")

    mailbox_folder = get_gmail_folder()
    
    print("Connecting to Gmail...")
    notifications = []
    
    try:
        with IMAPClient('imap.gmail.com', use_uid=True) as server:
            server.login(EMAIL, PASSWORD)
            server.select_folder(mailbox_folder, readonly=True)
            
            messages = server.search('UNSEEN')
            print(f"📥 Found {len(messages)} unread release notifications.\n")
            
            if messages:
                # Apply the processing limit when provided.
                message_list = messages[:limit] if limit else messages
                print("Parsing notifications:")
                
                for msg_id, data in server.fetch(message_list, ['ENVELOPE']).items():
                    envelope = data[b'ENVELOPE']
                    # Use type: ignore because IMAPClient.fetch() has loose typing.
                    subject = envelope.subject if hasattr(envelope, 'subject') else None  # type: ignore
                    
                    if subject is None:
                        print(f" ⚠️ Could not extract subject from envelope")
                        continue
                    
                    if isinstance(subject, bytes):
                        subject = subject.decode('utf-8', errors='ignore')

                    # Decode MIME-encoded subjects into readable text.
                    subject = decode_email_subject(subject)

                    repo, tag, release_type = parse_github_subject(subject)

                    if repo and tag:
                        print(f" 🚀 Found Update! Repo: {repo} | Tag: {tag} | Type: {release_type}")
                        notifications.append({
                            "repo": repo,
                            "tag": tag,
                            "release_type": release_type,
                            "email_id": msg_id
                        })
                    else:
                        print(f" ⚠️ Could not parse subject: {subject}")
    
    except Exception as e:
        print(f"Error fetching notifications: {e}")
        raise
    
    return notifications


def mark_as_read_and_delete(email_ids):
    """Mark a list of emails as read and move them to Trash."""
    if not email_ids:
        return
        
    if not EMAIL or not PASSWORD:
        raise ValueError("EMAIL and PASSWORD environment variables must be set")

    mailbox_folder = get_gmail_folder()

    try:
        with IMAPClient('imap.gmail.com', use_uid=True) as server:
            server.login(EMAIL, PASSWORD)
            server.select_folder(mailbox_folder)
            
            # Normalize to a list in case a single ID is passed.
            if not isinstance(email_ids, list):
                email_ids = [email_ids]
                
            # Perform bulk read and move operations.
            server.set_flags(email_ids, [b'\\Seen'])
            server.move(email_ids, '[Gmail]/Trash')
            
    except Exception as e:
        print(f"Error marking emails as read/deleted: {e}")


def check_releases():
    """Run the legacy release check flow via get_pending_notifications."""
    notifications = get_pending_notifications()
    print(f"\nFound {len(notifications)} release notification(s).")


# Run this file directly for manual IMAP connectivity testing.
if __name__ == "__main__":
    check_releases()
