import requests
from bs4 import BeautifulSoup
import csv
from datetime import datetime
import re

# Define the cookie for GDPR compliance
COOKIES = {'DSGVO_ZUSAGE_V1': 'true'}

def extract_article_content(url):
    try:
        response = requests.get(url, cookies=COOKIES)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Error fetching URL {url}: {e}")
        return "", ""

    article = BeautifulSoup(response.text, 'html.parser')

    # Extract the author
    author = "kein Autor"  # Default value

    # Check in <header> for the author
    header = article.find("header")
    if header:
        author_span = header.find("span", class_="simple")
        if author_span:
            author = author_span.get_text(strip=True)
    
    # Check in <div class="article-origins"> for the author
    origins_div = article.find("div", class_="article-origins")
    if origins_div:
        author = origins_div.get_text(strip=True)

    # Clean and split author names
    if author != "kein Autor":
        # Replace various separators with comma
        author = re.sub(r'\s*(,|oder|&|/|und)\s*', ',', author)
        # Split and clean each author name
        authors = [auth.strip() for auth in author.split(',') if auth.strip()]
        # Join with comma and space
        author = ", ".join(authors)

    # Extract the article body
    article_body = article.find("div", class_="article-body")
    if article_body is None:
        return "", author

    # Collect the text content, skipping paragraphs within <aside> tags
    content = []
    for para in article_body.find_all("p"):
        # Check if the paragraph is inside an <aside> element
        if para.find_parent("aside") is not None:
            continue
        content.append(para.get_text(strip=True))

    return " ".join(content), author

def save_to_csv(file_path, data):
    with open(file_path, "a", newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        writer.writerows(data)
    print(f"Saved {len(data)} new URLs to {file_path}")

def read_seen_urls(file_path):
    seen_urls = set()
    try:
        with open(file_path, newline='', encoding='utf-8') as file:
            reader = csv.reader(file)
            next(reader)  # Skip header
            for row in reader:
                if row:  # Skip empty rows
                    seen_urls.add(row[0])
    except FileNotFoundError:
        pass
    return seen_urls

def crawl_sitemap(url):
    collected_data = []
    seen_urls = read_seen_urls("urls.csv")

    print(f"Crawling Sitemap URL: {url}")

    try:
        response = requests.get(url, cookies=COOKIES)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Error fetching sitemap {url}: {e}")
        return collected_data

    sitemap_content = response.content
    soup = BeautifulSoup(sitemap_content, 'xml')
    url_tags = soup.find_all('url')

    if not url_tags:
        return collected_data

    for url_tag in url_tags:
        loc = url_tag.find('loc').text
        if loc in seen_urls:
            continue

        lastmod = url_tag.find('lastmod').text
        print(f"Processing Article URL: {loc}")
        content, author = extract_article_content(loc)

        # Only add to collected_data if content is not empty
        if content:
            collected_data.append((loc, lastmod, author, content))
            seen_urls.add(loc)

    # Save collected data to CSV after processing this sitemap
    if collected_data:
        save_to_csv("urls.csv", collected_data)

    return collected_data

# Determine the current year and month
current_year = datetime.now().year
current_month = datetime.now().month

# Set the start year
start_year = 2013

# Generate and crawl sitemap URLs from January 2013 to the current date
sitemap_urls = []
for year in range(start_year, current_year + 1):
    for month in range(1, 13):
        if year == current_year and month > current_month:
            break
        sitemap_url = f"https://www.derstandard.at/sitemaps/sitemap-{year}-{month:02}.xml"
        sitemap_urls.append(sitemap_url)

# Crawl each sitemap URL
for sitemap_url in sitemap_urls:
    crawl_sitemap(sitemap_url)

# Final count of collected data
print(f"Total new URLs collected: {len(read_seen_urls('urls.csv'))}")
