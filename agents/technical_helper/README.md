# Hold My Hand Technical Helper

## Summary
The decision-maker behind Hold My Hand's shopping help. It turns a shopper's needs into searches, checks the
real listings the Hold My Hand Scraper returns against those needs, and picks one product with reasons, or
says honestly that nothing fit. It also explains phone, computer and website problems one step at a time.

## What it does
- Takes structured shopping tasks from the Hold My Hand Orchestrator (only from its address).
- Asks the Scraper for up to three listings per search, then checks each one:
  - hard requirements first: the right kind of product (not an accessory), price shown and within budget
    in the same currency, in stock, and any must-have feature stated in the listing itself;
  - then a published weighted score for the rest: features the listing mentions, star rating, number of
    ratings, stock, budget left over, and a small penalty for sponsored listings.
- Searches again with tighter terms when nothing is good enough, at most twice (three searches in all).
- Returns exactly one product (name, brand, price and currency seen, link, reasons, what the listing
  didn't say) or a no_match with the reasons, e.g. "4 cost more than your budget".
- Answers how-to questions step by step; when a web address is given it asks the Scraper to read that
  public page so it can name the real links.

## Constraints and scope
- Never reads web pages or runs Apify itself; all listings come from the Scraper.
- Never invents prices, ratings, features or links. Optional Groq wording is discarded if it mentions any
  number or link that isn't in the listing.
- Never buys anything or asks for passwords, card numbers or codes. Warns about likely scams.
- Shopping requests sent directly in chat are pointed to the Orchestrator.

## Example prompts
- "My printer says it's offline. What do I do?"
- "How do I make the text bigger on my iPhone?"
- "Where do I click to pay my bill on https://www.example.com?"

## Keywords
Hold My Hand, product evaluation, requirement checking, budget filter, accessible products, seniors, tech
support, step by step help, scam warning, Fetch.ai, Agentverse, multi-agent
