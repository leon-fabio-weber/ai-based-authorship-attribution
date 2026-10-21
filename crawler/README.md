# Historical Article Crawler (2025)

This directory contains the original **DerStandard article crawler** used for the 2025 diploma thesis project, *AI-Based Authorship Attribution*.

The crawler collects German-language news articles from DerStandard by traversing monthly XML sitemaps, retrieving individual article pages, extracting author information and article text, and storing the resulting records in a CSV file.

This implementation represents the historical article collection stage of the project. The collected articles were intended for subsequent database ingestion, dataset preparation, and BERT-based authorship classification.

The crawler is preserved as a reference to the original implementation. It is separate from the automated Apache Airflow pipeline developed in 2026.

## 1. Processing Architecture

The crawler follows a sequential workflow:

```text
                         Start Script
                              |
                              v
                  Determine Current Date
                              |
                              v
                Generate Monthly Sitemap URLs
                              |
                              v
                 Iterate Over Each Sitemap
                              |
                              v
                    Read Existing CSV
                              |
                              v
                     Download Sitemap
                              |
                              v
                      Parse XML <url>
                              |
                              v
                   Extract Article URL
                              |
                              v
                    URL Already Seen?
                       /          \
                     Yes           No
                      |             |
                      v             v
                    Skip      Request Article
                                    |
                                    v
                             Extract Author
                                    |
                                    v
                             Extract Content
                                    |
                                    v
                            Content Non-Empty?
                               /        \
                              No        Yes
                              |          |
                              v          v
                            Skip     Add Record
                                         |
                                         v
                              Continue Sitemap
                                         |
                                         v
                               Append Records
                                  to urls.csv
                                         |
                                         v
                               Next Sitemap
```

The implementation processes sitemap files and article pages sequentially. It does not use asynchronous requests, multiple worker processes, or a task scheduler.

## 2. Generating Monthly Sitemap URLs

The crawler begins by determining the current year and month:

```python
current_year = datetime.now().year
current_month = datetime.now().month
```

The historical implementation then defines the starting year:

```python
start_year = 2013
```

For every year between `start_year` and the current year, the script generates sitemap URLs for each month.

The URL pattern is:

```text
https://www.derstandard.at/sitemaps/sitemap-YYYY-MM.xml
```

For example:

```text
https://www.derstandard.at/sitemaps/sitemap-2013-01.xml
https://www.derstandard.at/sitemaps/sitemap-2013-02.xml
https://www.derstandard.at/sitemaps/sitemap-2013-03.xml
...
https://www.derstandard.at/sitemaps/sitemap-2024-01.xml
...
```

The nested loop processes years in ascending order and months from January through December.

For the current year, it stops generating sitemap URLs after the current month.

For example, if the script runs in October 2026, it generates monthly sitemap URLs from January 2013 through October 2026.

This would produce 166 sitemap URLs.

The URLs are first stored in the `sitemap_urls` list. The script then processes each entry:

```python
for sitemap_url in sitemap_urls:
    crawl_sitemap(sitemap_url)
```


The script does not use a sitemap index to discover available monthly files. It constructs the expected URLs directly.

## 3. Processing a Sitemap

Each monthly sitemap is processed by:

```python
crawl_sitemap(url)
```

The function begins by creating an empty list for newly collected article records:

```python
collected_data = []
```

It then reads the URLs already stored in the local CSV file:

```python
seen_urls = read_seen_urls("urls.csv")
```

This means the previously collected URLs are reloaded separately for every sitemap.

### 3.1 Requesting the Sitemap

The sitemap is downloaded using the Python `requests` library:

```python
response = requests.get(url, cookies=COOKIES)
response.raise_for_status()
```

The request includes the following cookie:

```python
COOKIES = {'DSGVO_ZUSAGE_V1': 'true'}
```

This is a hardcoded request cookie used by the historical implementation. It does not itself establish legal permission to collect or redistribute website content.

`raise_for_status()` raises an HTTP exception for unsuccessful HTTP response status codes.

If a request-related exception occurs, the crawler prints an error message and returns an empty collection for that sitemap.

The historical implementation does not configure an HTTP timeout, retry strategy, or explicit request delay.

### 3.2 Parsing the XML Sitemap

After a successful request, the response body is parsed as XML:

```python
soup = BeautifulSoup(sitemap_content, 'xml')
url_tags = soup.find_all('url')
```

The crawler searches for every `<url>` element in the sitemap.

A simplified sitemap entry might look like this:

```xml
<url>
    <loc>https://www.derstandard.at/example-article</loc>
    <lastmod>2025-03-12</lastmod>
</url>
```

The crawler expects each relevant entry to contain:

| XML element | Purpose |
|---|---|
| `<url>` | Container for one sitemap entry |
| `<loc>` | Article URL |
| `<lastmod>` | Last-modified value associated with the URL |

If the sitemap contains no `<url>` elements, the function returns without collecting any articles.

The crawler does not validate that a sitemap entry is an article before passing its URL to the article extraction function.

It also assumes that `<loc>` and `<lastmod>` exist. Missing elements can cause an exception because the code accesses their `.text` attributes directly.

## 4. Duplicate URL Detection

Before downloading an article, the crawler checks whether its URL has already been collected.

The function:

```python
read_seen_urls("urls.csv")
```

opens the existing CSV file and builds a Python `set` containing previously stored URLs.

The first field of each CSV row is interpreted as the article URL:

```python
seen_urls.add(row[0])
```

Using a set allows efficient membership checks.

During sitemap processing, the crawler extracts the URL:

```python
loc = url_tag.find('loc').text
```

It then checks:

```python
if loc in seen_urls:
    continue
```

If the URL is already present, the crawler skips that entry without downloading the article page.

If the URL is new, it continues with article extraction.

### Duplicate Detection Within a Sitemap

After successfully extracting non-empty article content, the crawler immediately adds the URL to the in-memory set:

```python
seen_urls.add(loc)
```

This prevents another identical URL within the same sitemap from being processed again after a successful extraction.

### Duplicate Detection Across Sitemaps

At the beginning of each sitemap, the crawler reloads the URL set from `urls.csv`.

Because successfully extracted articles are appended to the CSV after each sitemap, subsequent sitemap iterations can recognize those URLs.

The historical implementation does not query the MySQL database for duplicates.

### Known CSV Header Issue

There is an inconsistency between reading and writing CSV files.

`read_seen_urls()` always skips the first row:

```python
next(reader)
```

However, `save_to_csv()` writes article records without creating a header.

If `urls.csv` has no header, the first article record will be skipped when the file is read again.

Consequently, the URL in that first record may not be recognized as previously collected.

This behavior is preserved and documented as part of the historical implementation.

## 5. Downloading Individual Articles

For every URL that is not already in the seen-URL set, the crawler calls:

```python
content, author = extract_article_content(loc)
```

The function sends another HTTP request:

```python
response = requests.get(url, cookies=COOKIES)
response.raise_for_status()
```

The same cookie is included in the article request.

If a request-related exception occurs, the function prints an error message and returns:

```python
("", "")
```

The caller later rejects the article because its content is empty.

If the request succeeds, the response HTML is parsed using:

```python
article = BeautifulSoup(response.text, 'html.parser')
```

The crawler then extracts the author and article body separately.

## 6. Extracting Author Information

Author extraction uses two possible locations in the article HTML.

### 6.1 Default Author Value

The crawler begins with:

```python
author = "kein Autor"
```

This value is used when no author information can be found in the expected HTML elements.

### 6.2 Extracting the Author From the Header

The crawler first searches for the first `<header>` element:

```python
header = article.find("header")
```

If it exists, the crawler searches inside it for:

```html
<span class="simple">Author Name</span>
```

The author text is extracted using:

```python
author_span.get_text(strip=True)
```

This removes leading and trailing whitespace from the extracted text.

### 6.3 Checking the Article Origins Element

Next, the crawler searches for:

```html
<div class="article-origins">Author Name</div>
```

If this element exists, its text replaces the author value previously extracted from the header.

Therefore, the author extraction priority is:

1. `div.article-origins`, if present.
2. `header span.simple`, if present.
3. `"kein Autor"` as the fallback value.

The historical code does not independently verify whether the extracted text represents a valid author name.

### 6.4 Normalizing Multiple Authors

If an author value was found, the crawler normalizes several possible separators.

The following expression is used:

```python
re.sub(r'\s*(,|oder|&|/|und)\s*', ',', author)
```

It replaces the recognized separators with commas.

Supported separators include:

- Commas
- `oder`
- `&`
- `/`
- `und`

The resulting string is split at commas, surrounding whitespace is removed, and empty entries are discarded.

The names are then joined using a comma followed by a space.

For example:

```text
Anna Beispiel und Max Muster
```

becomes:

```text
Anna Beispiel, Max Muster
```

The implementation performs text-based normalization rather than structured author identification.

Because the regular expression does not require word boundaries around `oder` or `und`, it can also match these character sequences inside names or other text. This is a known limitation of the historical approach.

## 7. Extracting Article Content

After author extraction, the crawler searches for the article body:

```python
article_body = article.find("div", class_="article-body")
```

If this element does not exist, the function returns an empty content string together with the extracted author:

```python
return "", author
```

The caller will not save the article because its content is empty.

### 7.1 Collecting Paragraphs

If an article body exists, the crawler retrieves all paragraph elements inside it:

```python
article_body.find_all("p")
```

The paragraphs are processed in document order.

For each paragraph, the crawler checks whether it is nested inside an `<aside>` element:

```python
if para.find_parent("aside") is not None:
    continue
```

Paragraphs inside `<aside>` elements are skipped.

This is intended to avoid including certain supplementary page elements in the extracted article text.

### 7.2 Joining the Article Text

For every remaining paragraph, the crawler extracts text using:

```python
para.get_text(strip=True)
```

The extracted paragraph strings are added to a list.

Finally, the crawler joins them with spaces:

```python
" ".join(content)
```

For example, an article containing:

```html
<div class="article-body">
    <p>First paragraph.</p>
    <aside>
        <p>Related content.</p>
    </aside>
    <p>Second paragraph.</p>
</div>
```

produces:

```text
First paragraph. Second paragraph.
```

The crawler does not preserve paragraph boundaries in the final output.

It also does not perform additional linguistic preprocessing such as tokenization, lemmatization, or sentence segmentation.

Those operations belong to later stages of the machine-learning workflow.

## 8. Filtering Extracted Articles

After extraction, the sitemap crawler checks:

```python
if content:
```

Only articles with a non-empty content string are added to the output collection.

Each accepted article is represented by a tuple:

```python
(loc, lastmod, author, content)
```

The fields are:

1. Article URL
2. Sitemap last-modified value
3. Extracted author information
4. Extracted article text

An article can still be saved when its author is `"kein Autor"`, provided that its content is non-empty.

The crawler does not require a known author before saving an article.

If extraction returns an empty content string, the URL is not added to the seen-URL set.

Consequently, a later sitemap containing the same URL may cause another extraction attempt.

## 9. Writing Articles to CSV

New article records are collected in memory while one sitemap is processed.

After the sitemap's entries have been examined, the crawler checks whether the collection contains any records.

If it does, the following function is called:

```python
save_to_csv("urls.csv", collected_data)
```

The CSV file is opened in append mode:

```python
with open(file_path, "a", newline='', encoding='utf-8') as file:
```

The records are written using:

```python
writer.writerows(data)
```

### CSV Properties

| Property | Behavior |
|---|---|
| Filename | `urls.csv` |
| Encoding | UTF-8 |
| Write mode | Append |
| Header | Not written |
| Record format | `url,lastmod,author,content` |
| Save frequency | After each processed sitemap with new articles |

Because the filename is relative, the output location depends on the working directory from which Python was started.

The CSV is not automatically stored beside the Python script.

For example, running the script from the `crawler/` directory would use `crawler/urls.csv`.

The script does not automatically create a separate output directory or timestamped export.

It also does not directly import collected records into MySQL.

That is handled by a separate stage of the overall project.

**The resulting CSV can contain complete copyrighted article texts and must not be committed to the public repository.**

## 10. Completing the Crawl

After one sitemap finishes, the outer loop continues with the next generated monthly sitemap.

This process continues until every generated sitemap URL has been passed to `crawl_sitemap()`.

At the end, the script prints:

```python
print(f"Total new URLs collected: {len(read_seen_urls('urls.csv'))}")
```

Despite the message, this is not a count of articles newly collected during the current execution.

It counts the distinct URLs returned by `read_seen_urls()` from the local CSV file.

Therefore:

- Previously collected URLs contribute to the count.
- The first row is skipped by the CSV reader.
- The count may differ from the actual number of CSV records.
- The count does not necessarily represent the number of new articles collected in the current run.

This is an important distinction when interpreting historical console output.

## 11. Execution Characteristics and Limitations

The historical crawler was implemented as a sequential data collection script.

It has several characteristics that should be considered before attempting to reuse it.

| Area | Historical behavior |
|---|---|
| Execution | Starts automatically at module level |
| Sitemap discovery | Generates monthly URLs directly |
| Article requests | Sequential HTTP requests |
| HTTP timeouts | Not configured |
| Rate limiting | Not configured |
| Request retries | No explicit retry strategy |
| Article limit | Not configured |
| Duplicate detection | Local CSV URL set |
| Database integration | Not performed by this script |
| Output | Append-only CSV |
| Error handling | Catches request-related exceptions |
| HTML extraction | Depends on specific page elements |

### Important Execution Warning

**Importing this Python module starts the sitemap loop and can trigger live HTTP requests.**

Do not import or execute it during routine installation, tests, documentation generation, or application startup.

The script is retained as historical source code and is not certified to work with the current DerStandard website.

The absence of request timeouts, rate limiting, and a fixed article limit makes uncontrolled execution particularly undesirable.

The original implementation's behavior is documented here for transparency, not as a recommendation for current web crawling practices.

## 12. Historical Crawler vs. 2026 Airflow Pipeline

The historical crawler and the later Airflow crawler serve related purposes but are separate implementations.

| Feature | Historical crawler (2025) | Airflow extension (2026) |
|---|---|---|
| Execution | Standalone Python script | Scheduled Airflow DAG |
| Sitemap processing | Monthly sitemaps across a configured year range | Date-scoped article discovery |
| Article extraction | HTML parsing with BeautifulSoup | Separate extraction implementation |
| Output | Shared `urls.csv` | Run-specific CSV files |
| Duplicate handling | Previously seen URLs in CSV | Workflow-specific duplicate checks |
| HTTP timeouts | Not configured | Configured |
| Request delay | Not configured | At least one second between article requests |
| Database import | Separate workflow | Dedicated Airflow import task |
| Scheduling | No built-in scheduler | Daily at 01:00 Europe/Vienna |

The 2026 Airflow implementation is located at:

[`airflow/daily_pipeline/crawler.py`](../airflow/daily_pipeline/crawler.py)

It does not import or execute the historical crawler.

Both implementations are included to document the project's development from its original data collection approach to a later automated workflow.

## 13. Validation and Reproducibility

The historical extraction and CSV behavior was checked locally using synthetic HTML fixtures.

These checks did not validate:

- Compatibility with current live sitemap responses.
- Compatibility with current article-page HTML.
- Network failures and timeouts under real conditions.
- The completeness or correctness of author information.
- A complete end-to-end crawl against the live website.

The original article dataset and CSV exports are not distributed in this repository.

The crawler is therefore provided as a **historical implementation reference**, not as a verified production-ready collection system.

## Related Documentation

- [Project Overview](../README.md)
- [MySQL Database](../mysql/README.md)
- [BERT Training](../docs/TRAINING.md)
- [2026 Airflow Pipeline](../airflow/README.md)