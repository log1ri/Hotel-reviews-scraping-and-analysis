from dotenv import load_dotenv
from google.cloud import bigquery
from apify_client import ApifyClient
import os, re, logging, datetime, hashlib, pandas as pd

# Load environment variables and set up logging.
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hotel-scraper")


def default_post_date(lookback_months: int) -> str:
    """Return the first day of the month N months ago, used as the default POST_DATE filter."""
    today = datetime.date.today()
    idx = (today.year * 12 + today.month - 1) - lookback_months
    year, month = divmod(idx, 12)
    return f"{year:04d}-{month + 1:02d}-01"

def _get_int(name: str, default: int) -> int:
    """Get an integer environment variable, or raise an error if it's not a valid integer."""
    val = os.getenv(name, str(default))
    try:
        return int(val)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer (got {val!r})") from exc

def get_config() -> dict:
    """Load and validate runtime configuration from environment variables."""
    
    # Apify settings
    api_token = os.getenv("API_TOKEN")
    hotel_urls = [url.strip() for url in os.getenv("HOTEL_URLS", "").split(",") if url.strip()]
    rating_set = [r.strip() for r in os.getenv("RATING_SET", "5,4,3,2,1").split(",") if r.strip()]
    languages = [l.strip() for l in os.getenv("LANGUAGE", "en").split(",") if l.strip()]
    lookback_months = _get_int("LOOKBACK_MONTHS", 2)
    post_date = os.getenv("POST_DATE", default_post_date(lookback_months))
    max_items = _get_int("MAX_ITEMS", 1000)

    # BigQuery settings
    gcp_project_id = os.getenv("GCP_PROJECT_ID")
    bq_dataset = os.getenv("BQ_DATASET")
    bq_table = os.getenv("BQ_TABLE")
    bq_location = os.getenv("BQ_LOCATION", "asia-southeast3")
    google_application_credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

    if not hotel_urls or not api_token:
        raise SystemExit("HOTEL_URLS (comma-separated) and API_TOKEN must be set in the environment")
    if not all([gcp_project_id, bq_dataset, bq_table]):
        raise SystemExit("GCP_PROJECT_ID, BQ_DATASET, BQ_TABLE must be set in the environment")
    
    return {
        "api_token": api_token,
        "hotel_urls": hotel_urls,
        "rating_set": rating_set,
        "post_date": post_date,
        "max_items": max_items,
        "languages": languages,
        "gcp_project_id": gcp_project_id,
        "bq_dataset": bq_dataset,
        "bq_table": bq_table,
        "bq_location": bq_location,
        "google_application_credentials": google_application_credentials,
    }
    
def hotel_name_from_url(url: str) -> str:
    """Extract hotel name from TripAdvisor URL."""
    match = re.search(r"-Reviews-(.+?)\.html", url)
    if not match:
        return url
    return match.group(1).replace("_", " ")
    
def scrape_hotel(client: ApifyClient, url: str, cfg: dict) -> list[dict]:
    """Scrape reviews for a single hotel using the Apify TripAdvisor Reviews actor."""
    hotel_name = hotel_name_from_url(url)
    log.info("Scraping: %s...", hotel_name)
    
    run_input = {
        "startUrls": [{ "url": url }],
        "maxItemsPerQuery": cfg["max_items"],
        "scrapeReviewerInfo": True,
        "lastReviewDate": cfg["post_date"],
        "reviewRatings": cfg["rating_set"],
        "reviewsLanguages": cfg["languages"],     # Preferred languages
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
    """Build a DataFrame from scraped reviews and add unique review IDs."""
    df = pd.DataFrame(all_rows, columns=[
        "hotel_name", "page_url", "reviewer", "review_title",
        "review_text", "date_of_stay", "rating",
    ])

    if df.empty:
        log.warning("No reviews scraped — returning empty DataFrame")
        return df

    def make_id(page_url, reviewer, date_of_stay, review_text):
        raw = f"{page_url}|{reviewer}|{date_of_stay}|{review_text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    df["review_id"] = [
        make_id(str(u), str(r), str(d), str(t))
        for u, r, d, t in zip(df["page_url"], df["reviewer"], df["date_of_stay"], df["review_text"], strict=False)
    ]
    df["scraped_at"] = pd.Timestamp.now(tz="UTC")

    return df

def prepare_bigquery_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare scraped reviews for BigQuery loading."""
    if df.empty:
        log.warning("No reviews scraped — returning empty DataFrame")
        return df
    
    bq_df = df.drop_duplicates(subset=["review_id"]).copy()
    
    bq_df["date_of_stay"] = pd.to_datetime(bq_df["date_of_stay"], errors="coerce").dt.date
    bq_df["date_of_stay"] = bq_df["date_of_stay"].where(pd.notna(bq_df["date_of_stay"]), None,)
    bq_df["rating"] = pd.to_numeric(bq_df["rating"], errors="coerce").astype("Int64")
    
    return bq_df[
        [
            "review_id", 
            "hotel_name", 
            "page_url", 
            "reviewer", 
            "review_title",
            "review_text", 
            "date_of_stay", 
            "rating", 
            "scraped_at"   
        ]
    ]

def load_to_bigquery(df: pd.DataFrame, cfg: dict) -> None:
    """Load scraped reviews into BigQuery, merging with existing data."""
    bq_df = prepare_bigquery_dataframe(df)
    if bq_df.empty:
        log.warning("No reviews to load into BigQuery — skipping load")
        return
    
    client = bigquery.Client(project = cfg["gcp_project_id"],location = cfg["bq_location"],)
    table_id = f"{cfg['gcp_project_id']}.{cfg['bq_dataset']}.{cfg['bq_table']}"
    staging_table_id = f"{table_id}_staging"
    
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    client.load_table_from_dataframe(bq_df, staging_table_id, job_config=job_config).result()
    log.info("Loaded %d rows into staging table %s", len(bq_df), staging_table_id)
    
    merge_sql = f"""
    MERGE `{table_id}` AS T
    USING `{staging_table_id}` AS S
    ON T.review_id = S.review_id
    WHEN NOT MATCHED THEN
        INSERT (review_id, hotel_name, page_url, reviewer, review_title, review_text, date_of_stay, rating, scraped_at)
        VALUES (S.review_id, S.hotel_name, S.page_url, S.reviewer, S.review_title, S.review_text, S.date_of_stay, S.rating, S.scraped_at)
    """
    
    client.query(merge_sql).result()
    log.info("Merged data into table %s", table_id)


def main():
    # Phase 1: Scrape reviews from TripAdvisor using Apify
    cfg = get_config()
    client = ApifyClient(cfg["api_token"])

    all_rows = []
    for url in cfg["hotel_urls"]:
        try:
            all_rows += scrape_hotel(client, url, cfg)
        except Exception:
            log.exception("Error scraping hotel %s", url)

    df = build_dataframe(all_rows)
    log.info("Total %d reviews across %d hotels", len(df), len(cfg["hotel_urls"]))
    
    # Phase 2: Load data into BigQuery
    load_to_bigquery(df, cfg)

if __name__ == "__main__":
    main()