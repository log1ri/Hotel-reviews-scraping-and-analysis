import os, re, logging, datetime, hashlib, pandas as pd
from apify_client import ApifyClient
from dotenv import load_dotenv 

# Load environment variables and set up logging.
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hotel-scraper")


def default_start_date(lookback_months: int) -> str:
    """
    First day of the month, N months ago.     
    Example: If today is 2026-06 and lookback is 2 -> '2026-04-01'.
    """
    today = datetime.date.today()
    idx = (today.year * 12 + today.month - 1) - lookback_months
    year, month = divmod(idx, 12)
    return f"{year:04d}-{month + 1:02d}-01"


# Load configuration from environment variables, with some defaults and validation.
def get_config() -> dict:
    """Load and validate runtime configuration from environment variables."""
    
    HOTEL_URLS = [url.strip() for url in os.getenv("HOTEL_URLS", "").split(",") if url.strip()]
    API_TOKEN = os.getenv("API_TOKEN")
    LOOKBACK_MONTHS = int(os.getenv("LOOKBACK_MONTHS", "2"))
    
    if HOTEL_URLS == [] or not API_TOKEN:
        raise SystemExit("HOTEL_URLS or API_TOKEN is empty — ...")
    
    return {
        "api_token":  API_TOKEN,
        "hotel_urls": HOTEL_URLS,
        "rating_set": os.getenv("RATING_SET", "5,4,3,2,1").split(","),
        "start_date": os.getenv("START_DATE",default_start_date(LOOKBACK_MONTHS)),
        "max_items": int(os.getenv("MAX_ITEMS", "1000")),
        "language": os.getenv("LANGUAGE", "en"),
    }
    
# Extract hotel name from the TripAdvisor URL for better logging and output.
def hotel_name_from_url(url: str) -> str:
    match = re.search(r"-Reviews-(.+?)\.html", url)
    if not match:
        return url
    return match.group(1).replace("_", " ")
    
# Scrape reviews for a single hotel using the Apify client and return them as a list of dictionaries.
def scrape_hotel(client: ApifyClient, url: str, cfg: dict) -> list[dict]:
    hotel_name = hotel_name_from_url(url)
    log.info("\nScraping: %s...", hotel_name)
    
    run_input = {
        "startUrls": [{ "url": url }],
        "maxItemsPerQuery": cfg["max_items"],
        "scrapeReviewerInfo": True,
        "lastReviewDate": cfg["start_date"],
        "reviewRatings": cfg["rating_set"],
        "reviewsLanguages": [cfg["language"]],     # Preferred language
    }
    
    run = client.actor("maxcopell/tripadvisor-reviews").call(run_input=run_input, logger=None)
    
    rows = []
    for item in client.dataset(run.default_dataset_id).iterate_items():
        rows.append({
            "hotel_name":   hotel_name,
            "page_url":     item.get("url"),
            "reviewer":     (item.get("user") or {}).get("name"),
            "review_title": item.get("title"),
            "review_text":  item.get("text"),
            "date_of_stay": item.get("travelDate"),
            "rating":       item.get("rating"),
        })
    log.info("got %d reviews", len(rows))
    return rows


def build_dataframe(all_rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(all_rows, columns=[
        "hotel_name", "page_url", "reviewer", "review_title",
        "review_text", "date_of_stay", "rating",
    ])

    if df.empty:
        log.warning("No reviews scraped — returning empty DataFrame")
        return df

    def make_id(r):
        raw = f"{r['page_url']}|{r['reviewer']}|{r['date_of_stay']}|{r['review_text']}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    df["review_id"] = df.apply(make_id, axis=1)
    df["scraped_at"] = pd.Timestamp.now(tz="UTC")

    return df


def main():
    cfg = get_config()
    client = ApifyClient(cfg["api_token"])

    all_rows = []
    for url in cfg["hotel_urls"]:
        try:
            all_rows += scrape_hotel(client, url, cfg)
        except Exception as e:
            log.error("Error scraping hotel %s: %s", url, e)

    df = build_dataframe(all_rows)
    log.info("Total %d reviews across %d hotels", len(df), len(cfg["hotel_urls"]))
    # Phase 2: load_to_bigquery(df, cfg)

if __name__ == "__main__":
    main()