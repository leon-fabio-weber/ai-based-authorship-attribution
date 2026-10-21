"""Shared SQLAlchemy schema definitions and historical author-name parsing.

The ORM maps the article, author and many-to-many association tables used by the
database import and daily pipeline. Importing this module declares metadata only;
it does not create an engine, connect to MySQL or create tables.
"""

import re

from sqlalchemy import Column, ForeignKey, Integer, String, Table, Text
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import declarative_base, relationship


Base = declarative_base()

article_author_association = Table(
    "placeholder",
    Base.metadata,
    Column("article_id", Integer, ForeignKey("articles.id"), primary_key=True),
    Column("author_id", Integer, ForeignKey("authors.id"), primary_key=True),
)


class Article(Base):
    """ORM mapping for one article and its linked authors.

    ``url`` identifies the source page, ``lastmod`` preserves the source timestamp,
    and ``content`` stores the article body. ``authors`` uses the ``placeholder``
    association table; one article may therefore be linked to several authors.
    """
    __tablename__ = "articles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    url = Column(Text, nullable=False)
    lastmod = Column(String(100))
    content = Column(MEDIUMTEXT)

    authors = relationship(
        "Author", secondary=article_author_association, back_populates="articles"
    )

    def __repr__(self):
        """Return a compact representation that truncates long article URLs."""
        return f"<Article(id={self.id}, url='{self.url[:50]}...')>"


class Author(Base):
    """ORM mapping for a unique author name and its linked articles.

    The database enforces uniqueness on ``name``. ``articles`` is the reverse
    side of the many-to-many relationship through ``placeholder``.
    """
    __tablename__ = "authors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)

    articles = relationship(
        "Article", secondary=article_author_association, back_populates="authors"
    )

    def __repr__(self):
        """Return the author's database identifier and display name."""
        return f"<Author(id={self.id}, name='{self.name}')>"


def extract_authors(author_text):
    """Split a byline into normalized author names using the project's rules.

    Args:
        author_text: Raw byline text, optionally prefixed with ``Verfasst von``.

    Returns:
        A list of non-empty, whitespace-normalized author names with duplicates
        removed while preserving their first-seen order.

    Parsing recognizes commas, ``und``, location phrases introduced by ``aus``
    and a legacy title-case concatenation heuristic. The heuristic is deliberately
    retained for compatibility with the source data and is not a general-purpose
    name parser.
    """
    # Remove the byline prefix before applying the author separators.
    author_text = re.sub(r"^Verfasst von\s+", "", author_text.strip())

    authors = []

    if "," in author_text or " und " in author_text:
        # Normalize the conjunction so both separators use the same parsing path.
        author_text = author_text.replace(" und ", ", ")
        raw_authors = [a.strip() for a in author_text.split(",")]

        for raw_author in raw_authors:
            # Remove a trailing location phrase from each comma-separated name.
            aus_match = re.search(r"(.*?)\s+aus\s+(.*)", raw_author)
            if aus_match:
                authors.append(aus_match.group(1).strip())
            else:
                authors.append(raw_author.strip())
    else:
        # A single byline can contain several names separated by location phrases.
        aus_matches = re.findall(r"(.*?)\s+aus\s+[^\s]+(?:\s+|$)", author_text)
        if aus_matches:
            current_pos = 0
            for match in re.finditer(r"(.*?)\s+aus\s+[^\s]+(?:\s+|$)", author_text):
                author_name = match.group(1).strip()
                if author_name:
                    authors.append(author_name)
                current_pos = match.end()

            # Keep any author text that follows the final location phrase.
            if current_pos < len(author_text):
                rest = author_text[current_pos:].strip()
                if rest:
                    authors.append(rest)
        else:
            # Split concatenated title-cased names when no explicit separator exists.
            potential_names = re.findall(
                r"([A-Z][a-z]+)([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)", author_text
            )
            if potential_names:
                for first_name, last_name in potential_names:
                    authors.append(f"{first_name} {last_name}")
            else:
                # Without a recognized separator, retain the byline as one author.
                authors.append(author_text)

    # Normalize whitespace and remove empty or repeated author names.
    processed_authors = []
    for author in authors:
        author = re.sub(r"\s+", " ", author).strip()
        if author and author not in processed_authors:
            processed_authors.append(author)

    return processed_authors
