# Hold My Hand Orchestrator

## Summary
A patient shopping and tech-help assistant for older adults. Tell it what you need and your budget; it
checks real listings on Amazon.com through its helper agents and comes back with one product, its price
when it looked, a link and why it fits. It never buys anything: you decide.

## What it does
- Reads plain requests such as "a lightweight vacuum that's easy to use, under $100": the kind of product,
  budget and range, must-have features, and the shop.
- Asks one short question when something important is missing.
- Hands the research to the Hold My Hand Technical Helper, which checks real listings collected by the Hold My
  Hand Scraper, and replies with one product, or explains clearly why nothing fit.
- Offers "show me another" and "cheaper" follow-ups.
- Answers device and website questions step by step, through the Technical Helper.
- Lets you add a caregiver who may adjust a few display settings you choose (simpler wording, shorter
  explanations, larger-text hints, language, checking before bigger steps, pointing out clear features).
  You approve with a yes, see every change, and can say "undo caregiver changes" or "remove my caregiver".
- Tells you plainly when product search or storage isn't available.

## Constraints and scope
- Searches Amazon.com only, in US dollars. Prices and stock can change after it looks.
- Never places orders, takes payments or asks for passwords or card numbers.
- Recommends only products with real listing evidence; it says when a detail isn't shown.
- A caregiver can't change budgets, shops, purchases or which product is recommended.
- One product search per person at a time. Say "start over" to begin again.

## Example prompts
- "Find a lightweight vacuum that is easy to use under $100"
- "I need a blood pressure monitor with a large, easy-to-read display, budget $60"
- "Help me buy a phone with big buttons for my mother, between $50 and $150"
- "Show me another one"
- "How do I attach a photo to an email?"
- "Add a caregiver"

## Keywords
Hold My Hand, shopping assistant for seniors, older adults, accessible shopping, product recommendation,
Amazon product finder, budget shopping help, easy to use products, tech help for seniors, caregiver settings,
plain language assistant, Fetch.ai, Agentverse, ASI:One, multi-agent
