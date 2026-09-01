# Walmart Scraper

## What this does

This tool grabs product details off a Walmart page — title, price, condition,
barcode, and so on — and saves them in one place instead of you copying and
pasting each one by hand.

You browse Walmart normally in Chrome. When you're on a product page (or a
page of search results), you click one button in a small extension, and it
pulls the data off the page you're already looking at. It works on
walmart.com, walmart.ca, and walmart.com.mx.

Every product it saves goes into:

- **A spreadsheet file on your computer** (`backend/data/products.csv`), and
- **A Google Sheet**, if you set that up (see below) — one sheet per country,
  kept automatically up to date and de-duplicated.

If a product's title is in Spanish (from walmart.com.mx), it's automatically
translated into English before it lands in the sheet.

## What you need

- **A Mac, Windows, or Linux computer.**
- **Python**, version 3.10 or newer. If you're not sure whether you have it,
  open a terminal and type `python3 --version`.
- **Google Chrome.**
- **A Google account**, only if you want the Google Sheets part. You can skip
  this and just use the CSV file if you'd rather not set that up.

You don't need to know how to code to follow the setup below — it's copy,
paste, and run a couple of commands in a terminal window.

## Setting it up

1. **Get the code onto your computer** (download or `git clone` this
   repository), then open a terminal in that folder.

2. **Set up a private workspace for the project and install what it needs:**

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r backend/requirements.txt
   ```

   This keeps the project's packages in their own folder instead of mixing
   them into the rest of your computer. On Windows, use `.venv\Scripts\pip`
   instead of `.venv/bin/pip`.

3. **Start the server:**

   ```bash
   .venv/bin/python -m uvicorn app:app --reload --port 8000 --app-dir backend
   ```

   On Windows, use `.venv\Scripts\python` instead of `.venv/bin/python`.

   Leave this terminal window open — it needs to keep running while you use
   the extension. To stop it later, click into that window and press
   `Ctrl+C`.

4. **Load the Chrome extension:**
   - Go to `chrome://extensions` in Chrome.
   - Turn on **Developer mode** (top right).
   - Click **Load unpacked** and select the `chromefrontend` folder from this
     project.
   - A new icon appears in your Chrome toolbar — that's it.

## Using it

1. Go to any Walmart product page, or a Walmart search results page.
2. Click the extension's icon in your toolbar.
3. Click the button in the popup.

That's it — the product (or every product on that results page) is saved. The
popup shows you what it found, and it'll tell you if anything went to your
Google Sheet too.

## Setting up Google Sheets (optional)

If you just want the CSV file, skip this section entirely — everything above
already works without it.

To have every scrape land in a Google Sheet automatically:

1. Go to the [Google Cloud console](https://console.cloud.google.com/) and
   create a project (or use one you already have).
2. Turn on the **Google Sheets API** for that project.
3. Under **IAM & Admin → Service Accounts**, create a service account. This
   is a "robot" Google account the tool uses to write to your sheet — you
   don't need to give it any special permissions there.
4. On that service account's **Keys** tab, click **Add key → Create new key →
   JSON**, and save the file it downloads somewhere safe on your computer
   (not inside this project folder).
5. Open that downloaded file and copy the `client_email` value — it looks
   like `something@your-project.iam.gserviceaccount.com`.
6. Create a Google Sheet for each Walmart country you plan to scrape, and
   **share each one** with that email address, giving it Editor access. This
   is the step people forget — without it, the tool has no permission to
   write anything.
7. Copy each sheet's ID out of its URL — it's the long string between `/d/`
   and `/edit`.

Then, in the main project folder, create a file named `.env` (no filename
before the dot) with:

```
GOOGLE_SERVICE_ACCOUNT_FILE=/full/path/to/the/key/you/downloaded.json
GOOGLE_SHEET_ID_US=paste the US sheet's ID here
GOOGLE_SHEET_ID_CA=paste the CA sheet's ID here
GOOGLE_SHEET_ID_MX=paste the MX sheet's ID here
```

You only need to set an ID for the countries you actually scrape. Restart the
server (step 3 above) after saving this file.

Translating Spanish titles from walmart.com.mx uses this same setup — just
also turn on the **Cloud Translation API** in that same Google Cloud project,
and under **IAM & Admin → IAM**, grant that service account the
**Cloud Translation API User** role.

## A couple of things worth knowing

- Everything runs on your own computer — nothing is sent anywhere except to
  the Walmart page you're already viewing and, if you set it up, your own
  Google Sheet.
- Only scrape pages you're actually browsing yourself. Walmart's terms of
  service don't allow automated, high-volume collection.
- If Google Sheets is unreachable or not set up, scraping still works fine —
  your data just stays in the CSV file until you push it up later.
