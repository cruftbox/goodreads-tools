import requests
import time
from bs4 import BeautifulSoup
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, ElementClickInterceptedException
import os
import logging
from urllib.parse import quote

from config_loader import load_config

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('sync_log.txt'),
        logging.StreamHandler()
    ]
)

class BookSyncAutomation:
    def __init__(self, goodreads_user_id, storygraph_email, storygraph_password):
        self.goodreads_user_id = goodreads_user_id
        self.storygraph_email = storygraph_email
        self.storygraph_password = storygraph_password
        self.driver = None
        
    def get_recently_read_goodreads(self):
        """Fetch recently read books from Goodreads RSS feed"""
        try:
            logging.info("Fetching Goodreads RSS feed...")
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
            }
            rss_url = f"https://www.goodreads.com/user/updates_rss/{self.goodreads_user_id}"
            logging.info(f"Accessing: {rss_url}")
            
            response = requests.get(rss_url, headers=headers, timeout=30)
            if response.status_code != 200:
                logging.error(f"Error accessing RSS feed. Status code: {response.status_code}")
                logging.error("Response content: %s", response.text[:500])
                raise Exception("Failed to access RSS feed")

            # Parse content
            soup = BeautifulSoup(response.text, 'lxml-xml')
            items = soup.find_all('item')
            logging.info(f"Found {len(items)} total items")
            
            recent_books = []
            for item in items:
                try:
                    desc_elem = item.find('description')
                    
                    if desc_elem:
                        desc_soup = BeautifulSoup(desc_elem.text, 'html.parser')
                        desc_text = desc_soup.get_text()
                    else:
                        desc_text = ""
                    
                    logging.debug(f"Processing description: {desc_text}")
                    
                    if "gave" in desc_text and "stars to" in desc_text:
                        parts = desc_text.split("stars to")
                        if len(parts) > 1:
                            title_part = parts[1].strip()
                            # Improved title extraction
                            book_title = title_part.split(" by ")[0].strip()
                            # Remove series information in parentheses if present
                            if " (" in book_title:
                                book_title = book_title.split(" (")[0].strip()
                            
                            pub_date = item.find('pubDate')
                            if pub_date:
                                date_text = pub_date.text
                                date_read = datetime.strptime(date_text, '%a, %d %b %Y %H:%M:%S %z').astimezone()
                                
                                book = {
                                    'title': book_title,
                                    'date_read': date_read
                                }
                                recent_books.append(book)
                                logging.info(f"Found rated book: {book_title} (Read on: {date_read.strftime('%Y-%m-%d')})")
                
                except Exception as e:
                    logging.error(f"Error processing item: {str(e)}")
                    continue

            if not recent_books:
                logging.info("No recently read books found")
            else:
                logging.info(f"Found {len(recent_books)} recently read books:")
                for book in recent_books:
                    logging.info(f"- {book['title']} (Read on: {book['date_read'].strftime('%Y-%m-%d')})")
            
            return recent_books

        except Exception as e:
            logging.error(f"Error fetching RSS feed: {str(e)}")
            raise

    def initialize_browser(self):
        """Initialize browser for StoryGraph interaction"""
        if not self.driver:
            chrome_options = webdriver.ChromeOptions()
            chrome_options.add_argument('--window-size=1920,1080')
            chrome_options.add_argument('--start-maximized')
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            
            self.driver = webdriver.Chrome(options=chrome_options)
            self.driver.implicitly_wait(10)

    def login_to_storygraph(self):
        """Login to StoryGraph with improved waits and verification"""
        self.initialize_browser()
        try:
            logging.info("Navigating to StoryGraph login page...")
            self.driver.get("https://app.thestorygraph.com/users/sign_in")
            
            # Wait for page to fully load
            WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(5)  # Additional wait for dynamic content
            
            logging.info("Checking if already logged in...")
            # Check if we're already logged in
            if "app.thestorygraph.com/" in self.driver.current_url and not "/sign_in" in self.driver.current_url:
                logging.info("Already logged in to StoryGraph")
                return
                
            logging.info("Entering email...")
            email_field = WebDriverWait(self.driver, 20).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='email']"))
            )
            email_field.clear()  # Clear any existing text
            email_field.send_keys(self.storygraph_email)
            time.sleep(1)  # Brief pause between inputs
            
            logging.info("Entering password...")
            password_field = WebDriverWait(self.driver, 20).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='password']"))
            )
            password_field.clear()  # Clear any existing text
            password_field.send_keys(self.storygraph_password)
            time.sleep(1)  # Brief pause before clicking
            
            logging.info("Clicking sign in button...")
            sign_in_button = WebDriverWait(self.driver, 20).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Sign in')]"))
            )
            # Try multiple click methods in case one fails
            try:
                sign_in_button.click()
            except:
                self.driver.execute_script("arguments[0].click();", sign_in_button)
            
            logging.info("Waiting for login to complete...")
            # Wait for successful login - check for both possible success indicators
            WebDriverWait(self.driver, 30).until(
                lambda driver: (
                    "app.thestorygraph.com/" in driver.current_url 
                    and not "/sign_in" in driver.current_url
                )
            )
            
            # Additional wait for homepage to load
            time.sleep(5)
            
            # Final verification
            current_url = self.driver.current_url
            if "app.thestorygraph.com/" in current_url and not "/sign_in" in current_url:
                logging.info("Successfully logged into StoryGraph")
            else:
                logging.error(f"Login may have failed. Current URL: {current_url}")
                # Take screenshot for debugging
                self.driver.save_screenshot("login_failed.png")
                raise Exception("Login failed - incorrect final URL")
            
        except Exception as e:
            logging.error(f"Error during StoryGraph login: {str(e)}")
            logging.error(f"Current URL: {self.driver.current_url}")
            # Take screenshot for debugging
            try:
                self.driver.save_screenshot("login_error.png")
                logging.info("Screenshot saved as login_error.png")
            except:
                logging.error("Could not save screenshot")
            raise

    def check_book_exists(self, book):
        """Check if book already exists in StoryGraph reading journal"""
        try:
            logging.info(f"Checking if '{book['title']}' exists in StoryGraph journal...")
            self.driver.get("https://app.thestorygraph.com/journal")
            
            # Wait for journal page to load
            WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(3)
            
            # Try to find the book title in the journal
            page_source = self.driver.page_source.lower()
            book_title_lower = book['title'].lower()
            
            if book_title_lower in page_source:
                logging.info(f"Book '{book['title']}' already exists in StoryGraph")
                return True
            
            logging.info(f"Book '{book['title']}' not found in StoryGraph")
            return False
            
        except Exception as e:
            logging.error(f"Error checking book existence: {str(e)}")
            return False

    def set_date(self, date):
        """Set a date using the three dropdown selectors with improved error handling"""
        logging.info(f"Setting date to {date.strftime('%Y-%m-%d')}...")
        
        try:
            # Select Day using ID with multiple attempts
            max_attempts = 3
            for attempt in range(max_attempts):
                try:
                    logging.info(f"Attempting to select day (attempt {attempt + 1})...")
                    day_select = WebDriverWait(self.driver, 10).until(
                        EC.element_to_be_clickable((By.ID, "read_instance_day"))
                    )
                    self.driver.execute_script("arguments[0].scrollIntoView(true);", day_select)
                    time.sleep(1)
                    
                    # Try to select using JavaScript
                    self.driver.execute_script(
                        f"arguments[0].value = '{date.day}'; arguments[0].dispatchEvent(new Event('change'));",
                        day_select
                    )
                    logging.info(f"Day {date.day} selected")
                    break
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    logging.warning(f"Day selection attempt {attempt + 1} failed: {str(e)}")
                    time.sleep(2)
            
            # Select Month with similar retry logic
            for attempt in range(max_attempts):
                try:
                    logging.info(f"Attempting to select month (attempt {attempt + 1})...")
                    month_select = WebDriverWait(self.driver, 10).until(
                        EC.element_to_be_clickable((By.NAME, "read_instance[month]"))
                    )
                    self.driver.execute_script(
                        f"arguments[0].value = '{date.month}'; arguments[0].dispatchEvent(new Event('change'));",
                        month_select
                    )
                    logging.info(f"Month {date.month} selected")
                    break
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    logging.warning(f"Month selection attempt {attempt + 1} failed: {str(e)}")
                    time.sleep(2)
            
            # Select Year with similar retry logic
            for attempt in range(max_attempts):
                try:
                    logging.info(f"Attempting to select year (attempt {attempt + 1})...")
                    year_select = WebDriverWait(self.driver, 10).until(
                        EC.element_to_be_clickable((By.NAME, "read_instance[year]"))
                    )
                    self.driver.execute_script(
                        f"arguments[0].value = '{date.year}'; arguments[0].dispatchEvent(new Event('change'));",
                        year_select
                    )
                    logging.info(f"Year {date.year} selected")
                    break
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    logging.warning(f"Year selection attempt {attempt + 1} failed: {str(e)}")
                    time.sleep(2)
            
            logging.info("Date selection complete")
            
        except Exception as e:
            logging.error(f"Error during date selection: {str(e)}")
            self.driver.save_screenshot("date_selection_error.png")
            raise

    def update_book_status(self, book):
        """Update book status on StoryGraph with improved error handling"""
        try:
            if self.check_book_exists(book):
                return
                
            logging.info(f"Adding '{book['title']}' to StoryGraph...")
            
            # URL encode the search term
            encoded_title = quote(book['title'])
            search_url = f"https://app.thestorygraph.com/browse?search_term={encoded_title}"
            self.driver.get(search_url)
            
            # Wait for search results
            WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(3)
            
            logging.info("Looking for book in search results...")
            results = WebDriverWait(self.driver, 20).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".book-title-author-and-series"))
            )
            
            book_found = False
            for result in results:
                try:
                    title_text = result.text.lower()
                    if book['title'].lower() in title_text:
                        logging.info(f"Found matching book: {result.text}")
                        book_found = True
                        
                        # Scroll the result into view
                        self.driver.execute_script("arguments[0].scrollIntoView(true);", result)
                        time.sleep(1)
                        break
                except:
                    continue
            
            if not book_found:
                raise Exception(f"Could not find book '{book['title']}' in search results")
            
            # Multiple attempts for expanding dropdown
            max_attempts = 3
            for attempt in range(max_attempts):
                try:
                    logging.info(f"Opening status dropdown... (attempt {attempt + 1})")
                    expand_button = WebDriverWait(self.driver, 10).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "button.expand-dropdown-button"))
                    )
                    expand_button.click()
                    time.sleep(2)
                    break
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    logging.warning(f"Dropdown expansion attempt {attempt + 1} failed: {str(e)}")
                    time.sleep(2)
            
            logging.info("Looking for read option...")
            read_button = WebDriverWait(self.driver, 20).until(
                EC.element_to_be_clickable((
                    By.CSS_SELECTOR, 
                    "div.read-status-dropdown-content form[action*='status=read'] button[type='submit']"
                ))
            )
            
            # Scroll the button into view and click it
            self.driver.execute_script("arguments[0].scrollIntoView(true);", read_button)
            time.sleep(1)
            
            logging.info("Clicking read button...")
            try:
                read_button.click()
            except ElementClickInterceptedException:
                self.driver.execute_script("arguments[0].click();", read_button)
            
            # Wait for the read status to be applied
            time.sleep(3)
            
            logging.info("Looking for 'No read date' text...")
            no_date_text = WebDriverWait(self.driver, 20).until(
                EC.element_to_be_clickable((
                    By.XPATH, 
                    "//p[contains(@class, 'text-darkerGrey') and contains(text(), 'No read date')]"
                ))
            )
            
            logging.info("Clicking 'No read date'...")
            try:
                no_date_text.click()
            except ElementClickInterceptedException:
                self.driver.execute_script("arguments[0].click();", no_date_text)
            
            time.sleep(2)
            
            logging.info("Setting completion date...")
            self.set_date(book['date_read'])
            
            # Find and click the Update button using exact HTML attributes
            logging.info("Looking for Update button...")
            
            update_button = WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((
                    By.CSS_SELECTOR, 
                    "input[type='submit'][name='commit'][value='Update'][data-disable-with='Update']"
                ))
            )
            
            # Ensure the button is in view and centered
            self.driver.execute_script("""
                arguments[0].scrollIntoView({
                    behavior: 'smooth',
                    block: 'center'
                });
            """, update_button)
            time.sleep(2)  # Wait for scroll and any animations
            
            # Attempt to click the button using the proven JavaScript method
            logging.info("Attempting to click Update button...")
            try:
                self.driver.execute_script("""
                    arguments[0].click();
                    arguments[0].form.submit();
                """, update_button)
                logging.info("Successfully clicked Update button using JavaScript")
            except Exception as e:
                logging.warning(f"JavaScript click failed: {str(e)}")
                # Fallback to regular click if JavaScript method fails
                try:
                    update_button.click()
                    logging.info("Successfully clicked Update button using regular click")
                except Exception as e:
                    logging.error(f"All click attempts failed: {str(e)}")
                    raise
            
            # Wait for update to complete
            time.sleep(5)
            logging.info(f"Successfully added '{book['title']}'")
            
        except Exception as e:
            logging.error(f"Error updating book status: {str(e)}")
            logging.error("Current URL: %s", self.driver.current_url)
            logging.info("Taking screenshot of error state...")
            try:
                self.driver.save_screenshot(f"book_error_{book['title'].replace(' ', '_')}.png")
                logging.info(f"Screenshot saved as book_error_{book['title'].replace(' ', '_')}.png")
            except:
                logging.error("Could not save screenshot")
            raise

    def sync_books(self):
        """Main sync function with improved error handling"""
        try:
            recent_books = self.get_recently_read_goodreads()
            
            if not recent_books:
                logging.info("No books to sync")
                return
                
            self.login_to_storygraph()
            
            for book in recent_books:
                try:
                    logging.info(f"\nProcessing book: '{book['title']}'")
                    self.update_book_status(book)
                    logging.info(f"Successfully processed '{book['title']}'")
                    time.sleep(3)  # Add delay between books
                except Exception as e:
                    logging.error(f"Error processing '{book['title']}': {str(e)}")
                    continue  # Continue with next book even if one fails
                    
        except Exception as e:
            logging.error(f"Sync error: {str(e)}")
        finally:
            if self.driver:
                logging.info("Closing browser...")
                self.driver.quit()

if __name__ == "__main__":
    try:
        # Load configuration (config.json, overridden by gitignored config.local.json if present)
        config_path = 'config.json'
        if not os.path.exists(config_path):
            logging.error(f"Config file not found at {config_path}")
            raise FileNotFoundError(f"Config file not found at {config_path}")

        config = load_config(config_path)

        required_keys = ['goodreads_user_id', 'storygraph_email', 'storygraph_password']
        missing_keys = [key for key in required_keys if key not in config]
        if missing_keys:
            raise KeyError(f"Missing required config keys: {', '.join(missing_keys)}")
        
        # Create sync bot instance
        sync_bot = BookSyncAutomation(
            goodreads_user_id=config['goodreads_user_id'],
            storygraph_email=config['storygraph_email'],
            storygraph_password=config['storygraph_password']
        )
        
        # Run the sync
        sync_bot.sync_books()
        
    except Exception as e:
        logging.error(f"Fatal error: {str(e)}")
        raise