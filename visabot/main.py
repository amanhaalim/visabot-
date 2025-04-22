import re
import logging
import time
import os
import json
import urllib.parse
import threading
import requests
from datetime import datetime
from queue import Queue
from bs4 import BeautifulSoup
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import logging.handlers
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()))

if not os.getenv("REPL_ID"):
    load_dotenv()

# Create logs directory
os.makedirs("logs", exist_ok=True)

# Set up rotating file handler
log_file = os.path.join("logs", "scraper.log")
handler = logging.handlers.RotatingFileHandler(
    log_file, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8"
)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)

# Configure logger - Set to ERROR level to suppress info messages
logger = logging.getLogger()
logger.setLevel(logging.ERROR)
logger.addHandler(handler)

# Base URLs
base_url = "https://ais.usvisa-info.com"
url = "https://ais.usvisa-info.com/en-ca/niv/users/sign_in"
url_after_login = "https://ais.usvisa-info.com/en-ca/niv/groups/"

# Global variables for real-time processing
json_queue = Queue()
processed_request_ids = set()
should_stop = False
login_active = False
driver = None
user_code = None
last_activity_time = time.time()
browser_restart_count = 0

# Date monitoring configuration
date_alerts_dir = "date_alerts"
os.makedirs(date_alerts_dir, exist_ok=True)
target_end_date = datetime(2026, 1, 1)  # Target end date: January 1, 2026
notified_dates = set()  # Keep track of dates we've already notified about

# Telegram configuration
telegram_enabled = True
telegram_bot_token = os.getenv('Token')

# Store multiple chat IDs for Telegram alerts
telegram_subscribers_file = os.path.join(date_alerts_dir, "telegram_subscribers.json")
telegram_subscribers = set()

# Load existing subscribers if file exists
if os.path.exists(telegram_subscribers_file):
    try:
        with open(telegram_subscribers_file, "r") as f:
            subscribers = json.load(f)
            telegram_subscribers = set(subscribers)
    except Exception as e:
        print(f"Error loading Telegram subscribers: {e}")

# Save the subscribers list to file
def save_telegram_subscribers():
    """
    Save the list of Telegram subscribers to a JSON file.
    """
    try:
        with open(telegram_subscribers_file, "w") as f:
            json.dump(list(telegram_subscribers), f)
        print(f"Saved {len(telegram_subscribers)} Telegram subscribers to file")
    except Exception as e:
        print(f"Error saving Telegram subscribers: {e}")

# Facility ID to location name mapping
facility_id_mapping = {
    "89": "Calgary",
    "90": "Halifax",
    "91": "Montreal",
    "92": "Ottawa",
    "93": "Quebec",
    "94": "Toronto",
    "95": "Vancouver"
}

# List of facility IDs to skip in dropdown selection
skip_facilities = ["91", "93"]  # Montreal and Quebec

# File to store already reported slots to prevent duplicate alerts after restarts
reported_slots_file = os.path.join(date_alerts_dir, "reported_slots.json")

# Load previously reported slots if file exists
if os.path.exists(reported_slots_file):
    try:
        with open(reported_slots_file, "r") as f:
            reported_slots = json.load(f)
            # Convert the list to a set for faster lookups
            notified_dates = set(reported_slots)
    except Exception as e:
        print(f"Error loading reported slots: {e}")
        reported_slots = []
else:
    reported_slots = []

def send_telegram_alert(message):
    """
    Send an alert message via Telegram bot API.
    Returns True if successful, False otherwise.
    """
    global telegram_enabled, telegram_bot_token, telegram_subscribers
    
    if not telegram_enabled or not telegram_bot_token:
        return False
    
    try:
        url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
        for chat_id in telegram_subscribers:
            data = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown"
            }
            response = requests.post(url, data=data)
            response.raise_for_status()
        return True
    except Exception as e:
        print(f"Error sending Telegram alert: {e}")
        return False

# Function to save reported slots to prevent duplicates after restart
def save_reported_slots():
    """
    Save the list of reported slots to a JSON file to prevent duplicates after restart.
    """
    try:
        with open(reported_slots_file, "w") as f:
            json.dump(list(notified_dates), f)
    except Exception as e:
        print(f"Error saving reported slots: {e}")

def parse_options(html):
    soup = BeautifulSoup(html, "html.parser")
    return [
        (option.text.strip(), option["value"])
        for option in soup.find_all("option")
        if option["value"].strip()  # Exclude empty values
    ]

def extract_code_with_regex(url):
    """
    Extracts the code from the URL using a regular expression.
    The code is assumed to be the digits between '/schedule/' and the next '/'.
    """
    pattern = r'/schedule/(\d+)/'
    match = re.search(pattern, url)
    if match:
        return match.group(1)
    else:
        return None

def setup_driver():
    """Set up and return a Chrome WebDriver with DevTools Protocol enabled."""
    import undetected_chromedriver as uc
    
    options = uc.ChromeOptions()
    # Add just the essential options, as some may not be compatible
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})  # Remove "performance"

    try:
        driver = uc.Chrome(options=options)
        print("undetected-chromedriver initialized successfully")
    except Exception as e:
        print(f"undetected-chromedriver initialization failed: {e}")
        raise
    
    # Set page load timeout
    driver.set_page_load_timeout(60)
    
    return driver

def create_output_directory():
    """Create and return a directory for saving JSON files."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = f"json_captures_{timestamp}"
    os.makedirs(output_dir, exist_ok=True)
    return output_dir

def check_for_dates_in_range(json_data, source_url, facility_id=None):
    """
    Check if the JSON contains dates within the target range.
    Returns a list of found dates that are in range.
    """
    today = datetime.now().date()
    found_dates = []
    
    try:
        # Handle different JSON structures
        
        # Case 1: List of date objects with direct date field
        if isinstance(json_data, list):
            for item in json_data:
                if isinstance(item, dict) and 'date' in item:
                    try:
                        date_str = item['date']
                        date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
                        
                        # Check if date is between today and target end date
                        if today <= date_obj <= target_end_date.date():
                            found_dates.append((date_str, item.get('business_day', True)))
                    except (ValueError, TypeError):
                        pass
        
        # Case 2: Date information might be nested deeper
        elif isinstance(json_data, dict):
            # Try to find dates at the top level
            if 'date' in json_data:
                try:
                    date_str = json_data['date']
                    date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
                    
                    if today <= date_obj <= target_end_date.date():
                        found_dates.append((date_str, json_data.get('business_day', True)))
                except (ValueError, TypeError):
                    pass
            
            # Look for arrays of dates
            for key, value in json_data.items():
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and 'date' in item:
                            try:
                                date_str = item['date']
                                date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
                                
                                if today <= date_obj <= target_end_date.date():
                                    found_dates.append((date_str, item.get('business_day', True)))
                            except (ValueError, TypeError):
                                pass
        
        # Print found dates or indicate no dates are available
        if found_dates:
            # Extract location information from URL if available
            location_info = "Unknown Location"
            
            # First try to use the provided facility_id
            if facility_id and facility_id in facility_id_mapping:
                location_info = facility_id_mapping[facility_id]
            else:
                # Try to extract from URL if not provided
                try:
                    facility_id_match = re.search(r'facility_id=([0-9]+)', source_url)
                    if facility_id_match:
                        extracted_id = facility_id_match.group(1)
                        if extracted_id in facility_id_mapping:
                            location_info = facility_id_mapping[extracted_id]
                    # If we still don't have a location, try to extract from the filename in the URL
                    elif "/" in source_url:
                        filename = source_url.split("/")[-1].split(".")[0]
                        if filename in facility_id_mapping:
                            location_info = facility_id_mapping[filename]
                        elif filename.isdigit() and filename in facility_id_mapping:
                            location_info = facility_id_mapping[filename]
                except Exception as e:
                    print(f"Error extracting location from URL: {e}")
            
            # Generate a unique identifier for each date to prevent duplicates
            # Format: date_location
            date_identifiers = [f"{date_str}_{location_info}" for date_str, _ in found_dates]
            
            # Filter out dates we've already notified about
            new_date_indices = [i for i, date_id in enumerate(date_identifiers) if date_id not in notified_dates]
            new_dates = [found_dates[i] for i in new_date_indices]
            new_date_ids = [date_identifiers[i] for i in new_date_indices]
            
            if new_dates:
                print("\n=====================================================")
                print(f"FOUND {len(new_dates)} NEW AVAILABLE DATE(S) at {location_info}:")
                
                for date_str, is_business_day in new_dates:
                    print(f"  - {date_str} (Business day: {is_business_day})")
                
                print("=====================================================")
                
                # Add new dates to notified set
                notified_dates.update(new_date_ids)
                
                # Save alert to file with timestamp
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                alert_file = os.path.join(date_alerts_dir, f"date_alert_{timestamp}.json")
                
                with open(alert_file, "w", encoding="utf-8") as f:
                    json.dump({
                        "timestamp": timestamp,
                        "source_url": source_url,
                        "location": location_info,
                        "found_dates": [{"date": date, "business_day": is_business} for date, is_business in new_dates]
                    }, f, indent=2)
                
                # Send Telegram alert if configured
                if telegram_enabled:
                    # Group dates by location
                    dates_by_location = {}
                    for i, (date_str, _) in enumerate(new_dates):
                        try:
                            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                            formatted_date = date_obj.strftime('%d/%m/%Y')
                        except:
                            formatted_date = date_str
                        
                        if location_info not in dates_by_location:
                            dates_by_location[location_info] = []
                        dates_by_location[location_info].append(formatted_date)
                    
                    # Send a single message for each location with all its dates
                    for loc, dates in dates_by_location.items():
                        if len(dates) == 1:
                            # Single date format
                            message = f"🔴Update🔴\nAppointment Alert\nSLOT AVAILABLE FOR :\nCity : {loc}\nDate : ({dates[0]})"
                        else:
                            # Multiple dates format
                            message = f"🔴Update🔴\nAppointment Alert\nMULTIPLE SLOTS AVAILABLE FOR :\nCity : {loc}\nDates : \n"
                            for date in dates:
                                message += f"- ({date})\n"
                        
                        send_telegram_alert(message)
                
                # Save reported slots to prevent duplicates after restart
                save_reported_slots()
        
        return found_dates
        
    except Exception as e:
        print(f"Error checking for dates: {e}")
        return []

def json_consumer_worker(output_dir):
    """
    Worker thread that consumes the JSON queue and saves them to files.
    Will keep running until should_stop is set to True and the queue is empty.
    Always overwrites existing JSON files with the same base name.
    """
    global should_stop, processed_request_ids, driver, last_activity_time
    
    while not should_stop:
        try:
            # Get item from queue with a timeout to allow checking should_stop
            try:
                request_id, filename_base, source_url, facility_id = json_queue.get(timeout=1)
                last_activity_time = time.time()  # Update activity timestamp
            except:
                # If queue is empty, just continue the loop
                continue
                
            # Skip if already processed
            if request_id in processed_request_ids:
                json_queue.task_done()
                continue
                
            # Create filepath (always overwrite)
            file_path = os.path.join(output_dir, f"{filename_base}.json")
                
            try:
                # Fetch the response body using CDP
                response = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
                body = response["body"]
                
                # Try to pretty print if it's valid JSON
                try:
                    json_content = json.loads(body)
                    
                    # Check for dates in the target range
                    check_for_dates_in_range(json_content, source_url, facility_id)
                    
                    # Save the JSON file
                    with open(file_path, "w", encoding="utf-8") as f:
                        json.dump(json_content, f, indent=2)
                        
                except json.JSONDecodeError:
                    # If not valid JSON, save as-is
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(body)
                
                processed_request_ids.add(request_id)
                
            except Exception as e:
                print(f"Error saving response body: {e}")
                
            finally:
                json_queue.task_done()
                
        except Exception as worker_error:
            print(f"Error in JSON consumer worker: {worker_error}")

def process_network_log(log, output_dir):
    """Process a single network log entry and queue it if it's JSON."""
    global processed_request_ids, last_activity_time
    
    try:
        log_dict = json.loads(log["message"])["message"]
        
        if log_dict["method"] != "Network.responseReceived":
            return
            
        response = log_dict["params"]["response"]
        url = response["url"]
        mime_type = response["mimeType"]
        
        # Check if the response is JSON
        if "json" in mime_type.lower() or url.endswith(".json"):
            request_id = log_dict["params"]["requestId"]
            
            # Skip if already processed
            if request_id in processed_request_ids:
                return
                
            # Sanitize the filename
            parsed_url = urllib.parse.urlparse(url)
            path = parsed_url.path
            filename = os.path.basename(path) if path else "response"
            
            if not filename.endswith(".json"):
                filename = filename.replace(".json", "")
            
            # Extract facility ID from the URL if present
            facility_id = None
            facility_id_match = re.search(r'facility_id=([0-9]+)', url)
            if facility_id_match:
                facility_id = facility_id_match.group(1)
            else:
                # Try to extract facility ID from filename (e.g., "95.json" for Vancouver)
                filename_id_match = re.match(r'^(\d+)$', filename)
                if filename_id_match and filename_id_match.group(1) in facility_id_mapping:
                    facility_id = filename_id_match.group(1)
            
            # Add to queue for processing
            json_queue.put((request_id, filename, url, facility_id))
            last_activity_time = time.time()  # Update activity timestamp
            
    except Exception as e:
        print(f"Error processing log entry: {e}")

def network_log_monitor(output_dir):
    """
    Monitor network logs in a separate thread.
    Will keep running until should_stop is set to True.
    """
    global should_stop, driver
    
    # Keep track of when we last processed logs
    last_log_time = time.time()
    
    while not should_stop:
        try:
            # Get new logs
            logs = driver.get_log("performance")
            
            if logs:
                last_log_time = time.time()
                
                # Process each log entry
                for log in logs:
                    process_network_log(log, output_dir)
                    
            # Sleep a bit to avoid hammering the CPU
            time.sleep(0.5)
            
        except Exception as e:
            print(f"Error monitoring network logs: {e}")
            time.sleep(1)  # Sleep a bit longer on error
    
    print("Network log monitor stopped")

def login(email, password):
    """
    Log in to the website.
    Returns True if login successful, False otherwise.
    """
    global driver, user_code, login_active, last_activity_time
    
    try:
        # Navigate to login page
        driver.get(url)
        print("Loading login page...")
        
        # Find and fill login elements
        email_field = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="user[email]"]'))
        )
        password_field = driver.find_element(By.CSS_SELECTOR, 'input[name="user[password]"]')
        
        # Fill in credentials
        email_field.send_keys(email)
        password_field.send_keys(password)
        print("Entering credentials...")
        
        # Use JavaScript to check the checkbox (bypasses the click interception)
        checkbox = driver.find_element(By.CSS_SELECTOR, 'input[name="policy_confirmed"]')
        driver.execute_script("arguments[0].click();", checkbox)
        
        # Submit using JavaScript too for good measure
        submit_button = driver.find_element(By.CSS_SELECTOR, 'input[type="submit"][name="commit"]')
        driver.execute_script("arguments[0].click();", submit_button)
        print("Logging in...")
        
        # Wait for login to complete
        WebDriverWait(driver, 30).until(
            lambda d: url_after_login in d.current_url
        )
        print("Login successful!")
        
        # Find the schedule link
        print("Looking for appointment schedule...")
        schedule = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href^="/en-ca/niv/schedule/"]'))
        )
        
        schedule_url = schedule.get_attribute("href").replace("_actions", "")
        user_code = extract_code_with_regex(schedule_url)
        print(f"Found appointment schedule")
        
        # Navigate to the schedule page
        print(f"Opening appointment schedule page...")
        driver.get(schedule_url)
        time.sleep(2)
        
        # Locate the dropdown element
        dropdown = Select(driver.find_element(By.ID, "appointments_consulate_appointment_facility_id"))

        # Iterate through all options (excluding the first empty one)
        for index in range(1, len(dropdown.options)):
            selected_option = dropdown.options[index]
            facility_id = selected_option.get_attribute("value")
            if facility_id in skip_facilities:
                continue
            print(f"Checking location: {selected_option.text}")
            dropdown.select_by_index(index)
            time.sleep(3)  # Wait 3 seconds to observe the selection
            
        login_active = True
        last_activity_time = time.time()  # Update activity timestamp
        return True
        
    except Exception as e:
        print(f"Login failed: {e}")
        login_active = False
        return False

def is_logged_in():
    """Check if we're still logged in."""
    global driver
    
    try:
        # Check if we're on a page that would indicate we're logged in
        current_url = driver.current_url
        if url_after_login in current_url:
            return True
            
        # Try to find an element that would only exist if logged in
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href^="/en-ca/niv/schedule/"]'))
        )
        return True
    except:
        return False

def restart_browser(email, password, output_dir):
    """Restart the browser completely and re-initialize everything."""
    global driver, should_stop, login_active, browser_restart_count
    
    print("Restarting browser...")
    browser_restart_count += 1
    
    try:
        # Close existing browser if it exists
        if driver:
            driver.quit()
            
        # Small delay to ensure resources are released
        time.sleep(5)
        
        # Initialize new browser
        driver = setup_driver()
        
        # Enable network interception via CDP
        driver.execute_cdp_cmd("Network.enable", {})
        
        # Login again
        login_result = login(email, password)
        if login_result:
            print(f"Browser restart complete and login successful")
        else:
            print(f"Browser restart complete but login failed")
        
        return login_result
    except Exception as e:
        print(f"Error restarting browser: {e}")
        return False

def health_check(max_inactivity_time=3600):
    """Check if the system appears to be working properly."""
    global last_activity_time
    
    current_time = time.time()
    time_since_activity = current_time - last_activity_time
    
    # If no activity for too long, system might be stuck
    if time_since_activity > max_inactivity_time:
        print(f"No activity detected for {time_since_activity:.1f} seconds")
        return False
        
    return True

def update_date_monitoring_config():
    """
    Update date monitoring configuration based on environment variables or defaults.
    """
    global target_end_date, telegram_enabled, telegram_bot_token
    
    # Try to get target end date from environment variable
    target_date_str = os.environ.get("TARGET_END_DATE")
    
    if target_date_str:
        try:
            # Parse the date string into a datetime object
            target_end_date = datetime.strptime(target_date_str, "%Y-%m-%d")
            print(f"Looking for dates between today and {target_date_str} (from config)")
        except ValueError:
            # Fallback to default if date format is invalid
            target_end_date = datetime(2026, 1, 1)
            print(f"Invalid date format in TARGET_END_DATE, using default: 2026-01-01")
    else:
        # Fallback to default
        target_end_date = datetime(2026, 1, 1)
        print(f"Looking for dates between today and 2026-01-01 (default)")
    
    # Update Telegram configuration from environment variables
    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    
    if telegram_bot_token:
        telegram_enabled = True
        print("Telegram alerts are enabled.")
    else:
        telegram_enabled = False
        print("Telegram alerts are disabled. Set TELEGRAM_BOT_TOKEN to enable.")

def continuous_monitoring(email, password, relogin_interval=300, browser_restart_interval=86400):
    """
    Run the monitoring process continuously, with periodic re-login and browser restart.
    
    Args:
        email: Email for login
        password: Password for login
        relogin_interval: Time in seconds between re-logins (default 5 minutes)
        browser_restart_interval: Time in seconds between full browser restarts (default 24 hours)
    """
    global should_stop, driver, login_active, last_activity_time
    
    try:
        print("\n============= US VISA APPOINTMENT MONITOR =============")
        print("This tool checks for available visa appointment dates")
        print("=========================================================\n")
        
        # Update date monitoring configuration
        update_date_monitoring_config()
        
        # Create a single output directory for the entire session
        output_dir = create_output_directory()
        print(f"Data will be saved to: {os.path.abspath(output_dir)}")
        print(f"Date alerts will be saved to: {os.path.abspath(date_alerts_dir)}")
        
        # Initialize the driver
        print("Initializing browser...")
        driver = setup_driver()
        
        # Enable network interception via CDP
        driver.execute_cdp_cmd("Network.enable", {})
        
        # Start the consumer thread
        consumer_thread = threading.Thread(
            target=json_consumer_worker, 
            args=(output_dir,),
            daemon=True
        )
        consumer_thread.start()
        
        # Start the network log monitor thread
        monitor_thread = threading.Thread(
            target=network_log_monitor, 
            args=(output_dir,),
            daemon=True
        )
        monitor_thread.start()
        
        # Start the Telegram bot worker thread
        telegram_thread = threading.Thread(
            target=telegram_bot_worker,
            daemon=True
        )
        telegram_thread.start()
        
        # Initial login
        if not login(email, password):
            print("Initial login failed, stopping.")
            return
            
        # Main loop - Keep running until manually stopped
        last_login_time = time.time()
        last_browser_restart_time = time.time()
        refresh_count = 0
        
        print("\nMonitoring has started. Press Ctrl+C to stop.\n")
        
        while not should_stop:
            try:
                current_time = time.time()
                time_since_login = current_time - last_login_time
                time_since_browser_restart = current_time - last_browser_restart_time
                
                refresh_count += 1
                print(f"Refresh #{refresh_count}: Checking for appointment dates... ({datetime.now().strftime('%H:%M:%S')})")
                
                # Check if it's time for browser restart (once a day by default)
                if time_since_browser_restart >= browser_restart_interval:
                    print(f"Time for scheduled browser restart (after {browser_restart_interval//3600} hours)")
                    
                    if restart_browser(email, password, output_dir):
                        last_login_time = time.time()
                        last_browser_restart_time = time.time()
                        print("Scheduled browser restart and re-login successful")
                    else:
                        print("Scheduled browser restart failed, will retry in 60 seconds")
                        time.sleep(60)
                        continue
                
                # Check if it's time to re-login or if we're logged out
                elif time_since_login >= relogin_interval or not is_logged_in():
                    print(f"Re-login required (after {relogin_interval//60} minutes)")
                    
                    # Clear cookies to simulate a fresh session without browser restart
                    driver.delete_all_cookies()
                    time.sleep(2)
                    
                    if login(email, password):
                        last_login_time = time.time()
                        print("Re-login successful")
                        
                        # Refresh the schedule page to generate new network activity
                        if user_code:
                            schedule_url = f"{base_url}/en-ca/niv/schedule/{user_code}"
                            driver.get(schedule_url)
                            print(f"Refreshed appointment schedule page")
                    else:
                        print("Re-login failed, attempting browser restart")
                        # Try a full browser restart as fallback
                        if restart_browser(email, password, output_dir):
                            last_login_time = time.time()
                            last_browser_restart_time = time.time()
                            print("Emergency browser restart successful")
                        else:
                            print("Emergency browser restart failed, will retry in 2 minutes")
                            time.sleep(120)
                            continue
                
                # Health check - if system appears stuck, restart browser
                if not health_check():
                    print("System appears to be stuck, restarting browser")
                    if restart_browser(email, password, output_dir):
                        last_login_time = time.time()
                        last_browser_restart_time = time.time()
                        print("System recovered successfully")
                    else:
                        print("Recovery failed, will retry")
                        time.sleep(60)
                
                # Sleep a bit to avoid tight looping
                time.sleep(10)
                
            except Exception as loop_error:
                print(f"Error in monitoring loop: {loop_error}")
                
                # Wait a bit before continuing
                time.sleep(30)
                
    except KeyboardInterrupt:
        print("\nMonitoring stopped by user")
    except Exception as main_error:
        print(f"Error during monitoring: {main_error}")
    finally:
        # Ensure proper cleanup
        should_stop = True
        if driver:
            driver.quit()
        print("Monitoring stopped")

def run_as_service():
    """
    Run the script as a persistent service with restart capability.
    This function never returns unless the program is forcibly terminated.
    """
    while True:
        try:
            # Get credentials from environment or config file for service mode
            email = os.environ.get("EMAIL") or input("Enter your email: ")
            password = os.environ.get("PASSWORD") or input("Enter your password: ")
            
            # Get relogin interval from environment or use default
            relogin_minutes_str = os.environ.get("RELOGIN_MINUTES") or "5"
            browser_restart_hours_str = os.environ.get("BROWSER_RESTART_HOURS") or "24"
            
            try:
                relogin_minutes = int(relogin_minutes_str)
                browser_restart_hours = int(browser_restart_hours_str)
            except ValueError:
                relogin_minutes = 5
                browser_restart_hours = 24
                
            relogin_interval = relogin_minutes * 60  # Convert to seconds
            browser_restart_interval = browser_restart_hours * 3600  # Convert to seconds
            
            # Check for Telegram configuration
            telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
            
            if telegram_bot_token:
                print("Telegram alerts are enabled for service mode")
            else:
                print("Telegram alerts are disabled. Set TELEGRAM_BOT_TOKEN environment variable to enable.")
            
            print(f"Starting continuous monitoring as service")
            print(f"Re-login interval: {relogin_minutes} minutes")
            print(f"Browser restart interval: {browser_restart_hours} hours")
            
            # Run the main monitoring function
            continuous_monitoring(email, password, relogin_interval, browser_restart_interval)
            
            # If we get here, monitoring has stopped for some reason
            # Wait a bit before restarting
            print("Monitoring stopped unexpectedly, restarting in 5 minutes...")
            time.sleep(300)
            
        except Exception as service_error:
            print(f"Critical service error: {service_error}")
            print("Restarting service in 5 minutes...")
            time.sleep(300)

def handle_telegram_command(message):
    """
    Handle Telegram bot commands and messages.
    Returns True if the message was handled, False otherwise.
    """
    global telegram_subscribers
    
    try:
        # Extract message data
        if 'message' not in message:
            return False
            
        chat_id = str(message['message']['chat']['id'])
        text = message['message'].get('text', '')
        
        # Handle /start command
        if text == '/start':
            # Add the user to subscribers if not already there
            if chat_id not in telegram_subscribers:
                telegram_subscribers.add(chat_id)
                save_telegram_subscribers()
            
            # Send welcome message
            welcome_message = f"🔴 Bot is running! 🔴\nYou will receive alerts when new visa appointment slots become available.\n Checking slot dates till {os.environ.get('TARGET_END_DATE')}"
            send_message_to_chat(chat_id, welcome_message)
            return True
            
        # Any other message also subscribes the user
        if chat_id not in telegram_subscribers:
            telegram_subscribers.add(chat_id)
            save_telegram_subscribers()
            welcome_message = "🔴 You've been subscribed! 🔴\nYou will receive alerts when new visa appointment slots become available."
            send_message_to_chat(chat_id, welcome_message)
            return True
            
        return False
    except Exception as e:
        print(f"Error handling Telegram command: {e}")
        return False

def send_message_to_chat(chat_id, message):
    """
    Send a message to a specific Telegram chat.
    """
    if not telegram_enabled or not telegram_bot_token:
        return False
    
    try:
        url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }
        response = requests.post(url, data=data)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Error sending Telegram message to {chat_id}: {e}")
        return False

def get_telegram_updates(offset=0):
    """
    Get updates from Telegram bot API.
    Returns a list of updates.
    """
    if not telegram_enabled or not telegram_bot_token:
        return []
    
    try:
        url = f"https://api.telegram.org/bot{telegram_bot_token}/getUpdates"
        params = {
            "offset": offset,
            "timeout": 30
        }
        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json().get('result', [])
    except Exception as e:
        print(f"Error getting Telegram updates: {e}")
        return []

def telegram_bot_worker():
    """
    Worker thread that checks for Telegram bot updates.
    Will keep running until should_stop is set to True.
    """
    global should_stop
    
    print("Starting Telegram bot worker...")
    last_update_id = 0
    
    while not should_stop:
        try:
            # Get updates with long polling
            updates = get_telegram_updates(last_update_id)
            
            for update in updates:
                # Process the update
                handle_telegram_command(update)
                
                # Update the last update ID
                if update['update_id'] >= last_update_id:
                    last_update_id = update['update_id'] + 1
            
            # Sleep a bit to avoid hammering the API
            time.sleep(1)
        except Exception as e:
            print(f"Error in Telegram bot worker: {e}")
            time.sleep(5)  # Sleep longer on error
    
    print("Telegram bot worker stopped.")

def send_telegram_alert(message):
    """
    Send an alert message via Telegram bot API to all subscribers.
    Returns True if successful, False otherwise.
    """
    global telegram_enabled, telegram_bot_token, telegram_subscribers
    
    if not telegram_enabled or not telegram_bot_token or not telegram_subscribers:
        return False
    
    try:
        url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
        success = True
        
        for chat_id in telegram_subscribers:
            data = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown"
            }
            try:
                response = requests.post(url, data=data)
                response.raise_for_status()
            except Exception as e:
                print(f"Error sending alert to {chat_id}: {e}")
                success = False
        
        return success
    except Exception as e:
        print(f"Error sending Telegram alerts: {e}")
        return False

if __name__ == "__main__":
    try:
        # Check if running in service mode
        service_mode = os.environ.get("SERVICE_MODE", "false").lower() == "true"
        
        if service_mode:
            print("Starting in service mode (continuous operation with auto-restart)")
            run_as_service()
        else:
            # Get login credentials
            # email = input("Enter your email: ")
            # password = input("Enter your password: ")
            # relogin_minutes = input("Enter minutes between re-logins (default: 5): ")
            # browser_restart_hours = input("Enter hours between browser restarts (default: 24): ")
            # custom_date = input("Enter custom target end date (YYYY-MM-DD) or leave blank for default (2026-01-01): ")

            email = os.getenv("EMAIL")
            password = os.getenv("PASSWORD")
            relogin_minutes = os.getenv("RELOGIN_MINUTES", "5")
            browser_restart_hours = os.getenv("BROWSER_RESTART_HOURS", "24")
            custom_date = os.getenv("TARGET_END_DATE", "2026-01-01")
            if custom_date:
                os.environ["TARGET_END_DATE"] = custom_date

            os.environ["TELEGRAM_BOT_TOKEN"] = telegram_bot_token
            
            # Set defaults if empty
            if not relogin_minutes.strip():
                relogin_minutes = "5"
            if not browser_restart_hours.strip():
                browser_restart_hours = "24"
                
            relogin_interval = int(relogin_minutes) * 60  # Convert to seconds
            browser_restart_interval = int(browser_restart_hours) * 3600  # Convert to seconds
            
            print(f"Starting monitoring with re-login every {relogin_minutes} minutes")
            print(f"Browser will restart every {browser_restart_hours} hours")
            
            continuous_monitoring(email, password, relogin_interval, browser_restart_interval)
            
    except Exception as e:
        print(f"Main program error: {e}")