# Flows

## Interactive search

```mermaid
sequenceDiagram
    actor U as You (Telegram)
    participant C as conversation.py
    participant L as Gemini
    participant A as app.py (background task)
    participant P as Pipeline (worker thread)
    participant K as Kleinanzeigen
    participant D as SQLite
    participant N as notify.py

    U->>C: "a good router in Wiesbaden, Mainz or Mannheim under 150"
    C->>L: next_turn(history)
    L-->>C: question or spec (places, one radius, quality guide)
    C-->>U: follow-up question (≤ 3)
    C-->>U: spec + [Search] [Change] [Cancel]
    U->>C: Search
    C->>A: start_search(chat, spec)
    A-->>U: status message, edited in place as each step finishes (at most every 3 s)
    A->>P: run_search_once (under lock, reports progress back)
    P->>D: insert spec
    loop each place
        P->>K: search page (s-suchanfrage.html)
    end
    P->>D: store every card once (out_of_range if price off or outside every place's radius)
    loop each in-range, not yet fetched listing
        P->>K: detail page (RequestGate pacing)
        P->>D: update listing (text, images, attributes)
    end
    loop each fetched listing without evaluation for this spec
        P->>P: facts → deterministic checks
        P->>L: judgment incl. product tier (strict JSON)
        P->>P: verify evidence, cap score by tier, apply threshold + hard fails
        P->>D: save evaluation (with exact prompt)
    end
    A->>N: send_search_results(chat, spec)
    N->>D: picks of this spec (seen before?)
    N-->>U: ranked list: score · title · price, town · km, tier, why + buttons [1] [2] …
    N->>D: record notifications (after success)
    A-->>U: summary + one link per place to save as Kleinanzeigen search
    U->>N: tap [n] (offers.py)
    N-->>U: that offer: photos, matches, concerns, unknowns, verdict, link
```

## Gmail monitoring

```mermaid
sequenceDiagram
    participant J as JobQueue (every GMAIL_POLL_INTERVAL_S)
    participant G as Gmail API (read-only)
    participant P as Pipeline (worker thread)
    participant K as Kleinanzeigen
    participant L as Gemini
    participant D as SQLite
    participant N as notify.py
    actor U as You (Telegram)

    J->>G: list messages (GMAIL_QUERY)
    loop each message not in gmail_messages
        J->>G: get message
        J->>J: extract listing links, match saved spec by query words
        J->>P: process_email(refs, spec or none)
        P->>D: store listings (source gmail)
        P->>K: detail pages (same RequestGate)
        P->>L: evaluate pending listings
        P->>D: save evaluations
        J->>D: mark email processed (only if complete)
    end
    J->>N: send_new_picks(notify chat)
    N-->>U: list of new picks with buttons, each listed once
    N->>D: record notifications
```
