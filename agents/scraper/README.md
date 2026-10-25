# Hold My Hand Scraper

## Summary
Collects real product listings for Hold My Hand. It runs an Apify Actor on an approved shopping site and
returns up to three listings with their price, brand, link, availability, rating and where each fact came
from. It reports facts only: the Hold My Hand Technical Helper decides which product, if any, fits.

## What it does
- Takes search requests only from the Hold My Hand Technical Helper's address.
- Runs the Apify Actor junglee/amazon-crawler (Amazon Product Scraper) for Amazon.com searches, with the
  budget passed as a price filter.
- Normalises each listing: title, brand, seller, price and currency, link (https://www.amazon.com/dp/...),
  availability, rating and number of ratings, up to five feature bullets, key attributes such as weight,
  image, whether it was sponsored, collection time, and which fields were missing.
- Drops duplicates, broken links, irrelevant results and listings over the budget; returns at most three and
  never pads the list.
- Reads one public web page for the Technical Helper when a person asks where to click on it.

## Constraints and scope
- Approved site: amazon.com only. Other sites are refused before anything runs.
- Every Actor run is limited: 6 items, a $0.05 charge cap, 75 seconds of run time, 20 runs per hour, and
  repeated searches within 10 minutes reuse the last result. The CAPTCHA solver is never used.
- No logins, personal data or private pages. Page reading refuses private and local network addresses and
  respects robots.txt.
- Without APIFY_API_TOKEN it returns a clear integration_unavailable error instead of any made-up listing.
- People who chat with it directly are pointed to the Hold My Hand Orchestrator; chat never starts a paid run.

## Example prompts
This agent works behind the Hold My Hand Orchestrator. To shop, chat with the Orchestrator, for example:
- "Find a lightweight vacuum that is easy to use under $100"
- "Look for a blood pressure monitor with a large display under $60"

## Keywords
Hold My Hand, Apify, Amazon product scraper, product listings, price extraction, product data, provenance,
shopping research, seniors, accessible shopping, Fetch.ai, Agentverse, multi-agent
