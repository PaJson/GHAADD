import os
import re
from dotenv import load_dotenv
from imapclient import IMAPClient

# Load credentials
load_dotenv()
EMAIL = os.getenv("GMAIL_USER")
PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

if not EMAIL or not PASSWORD:
    raise ValueError("GMAIL_USER and GMAIL_APP_PASSWORD environment variables must be set")

def parse_github_subject(subject):
    # This pattern captures everything after 'Release ' or 'Pre-release '
    # until the final ' - ' separator
    pattern = r"^\[([^\]]+)\]\s+(?:Pre-)?Release\s+(.+)(?:\s+-\s+.*)$"
    match = re.search(pattern, subject, re.IGNORECASE)
    
    if match:
        repo = match.group(1)
        tag = match.group(2)
        release_type = "Pre-release" if "Pre-release" in subject else "Release"
        return repo, tag, release_type
    
    # Fallback for subjects that might not have the " - " separator
    pattern_simple = r"^\[([^\]]+)\]\s+(?:Pre-)?Release\s+(.+)$"
    match_simple = re.search(pattern_simple, subject, re.IGNORECASE)
    if match_simple:
        repo = match_simple.group(1)
        tag = match_simple.group(2)
        release_type = "Pre-release" if "Pre-release" in subject else "Release"
        return repo, tag, release_type
        
    return None, None, None

def get_pending_notifications(limit=None):
    """
    Fetches unread GitHub release notifications from Gmail.
    Returns a list of dicts with keys: 'repo', 'tag', 'release_type', 'email_id'
    
    Args:
        limit: Maximum number of emails to process. None = all
    """
    # Validate environment variables
    if not EMAIL or not PASSWORD:
        raise ValueError("EMAIL and PASSWORD environment variables must be set")
    
    print("Connecting to Gmail...")
    notifications = []
    
    try:
        with IMAPClient('imap.gmail.com', use_uid=True) as server:
            server.login(EMAIL, PASSWORD)
            server.select_folder('GitHubNotifications', readonly=True)
            
            messages = server.search('UNSEEN')
            print(f"📥 Found {len(messages)} unread release notifications.\n")
            
            if messages:
                # Apply limit if specified
                message_list = messages[:limit] if limit else messages
                print("Parsing notifications:")
                
                for msg_id, data in server.fetch(message_list, ['ENVELOPE']).items():
                    envelope = data[b'ENVELOPE']
                    # Type: ignore because IMAPClient.fetch() has loose typing
                    subject = envelope.subject if hasattr(envelope, 'subject') else None  # type: ignore
                    
                    if subject is None:
                        print(f" ⚠️ Could not extract subject from envelope")
                        continue
                    
                    if isinstance(subject, bytes):
                        subject = subject.decode('utf-8', errors='ignore')
                    
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
    """
    Marks a list of emails as read and moves them to Trash.
    """
    if not email_ids:
        return
        
    if not EMAIL or not PASSWORD:
        raise ValueError("EMAIL and PASSWORD environment variables must be set")

    try:
        with IMAPClient('imap.gmail.com', use_uid=True) as server:
            server.login(EMAIL, PASSWORD)
            server.select_folder('GitHubNotifications')
            
            # Ensure it's a list even if a single ID is passed by mistake
            if not isinstance(email_ids, list):
                email_ids = [email_ids]
                
            # Perform bulk operations
            server.set_flags(email_ids, [b'\\Seen'])
            server.move(email_ids, '[Gmail]/Trash')
            
    except Exception as e:
        print(f"Error marking emails as read/deleted: {e}")

def check_releases():
    """Legacy function - now uses get_pending_notifications"""
    notifications = get_pending_notifications()
    print(f"\nFound {len(notifications)} release notification(s).")


if __name__ == "__main__":
    check_releases()