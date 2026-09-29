-- ===========================================================================
-- Flask OCR - SQLite schema + seed data
-- ===========================================================================
--
-- The SQLite store (DATABASE_BACKEND=sqlite) needs no server, no credentials and
-- no installation: app/sqlite.py creates everything below with
-- "CREATE TABLE IF NOT EXISTS" when a store connects from /database or at
-- start-up.  This file is the same DDL (plus the seed rows) as a plain script.
--
-- Usage:
--   sqlite3 instance/ocr_records.sqlite3 < sql/sqlite_schema.sql
--
-- or from Python, which is what the project itself uses (sqlite3 ships with
-- CPython, so there is nothing to install):
--
--   import sqlite3
--   sqlite3.connect("instance/ocr_records.sqlite3").executescript(
--       open("sql/sqlite_schema.sql", encoding="utf-8").read())
--
-- Names follow PAGES_TABLE_SUFFIX in app/database.py: the parent table is
-- SQLITE_TABLE (default `ocr_extractions`) and the per-page table is that name
-- plus `_pages` (default `ocr_extractions_pages`).  Everything is idempotent.
--
-- Timestamps are TEXT in UTC ("YYYY-MM-DD HH:MM:SS.ffffff"): sortable, and no
-- deprecated datetime adapter involved (see app/sqlite.py).
-- ===========================================================================

-- SQLite only enforces FOREIGN KEY ... ON DELETE CASCADE when this is set.  It
-- is a *per connection* switch, so set it in every session that writes
-- (the application sets it each time it connects).
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 1. ocr_extractions - one row per processed upload
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `ocr_extractions` (
  `id` INTEGER PRIMARY KEY AUTOINCREMENT,
  `filename` VARCHAR(255) NOT NULL,
  `uploaded_at` DATETIME NOT NULL,
  `content` TEXT NOT NULL,
  `kind` VARCHAR(16) NOT NULL DEFAULT 'image',
  `page_count` INTEGER NOT NULL DEFAULT 0,
  `char_count` INTEGER NOT NULL DEFAULT 0,
  `word_count` INTEGER NOT NULL DEFAULT 0,
  `confidence` REAL NULL,
  `duration_ms` INTEGER NOT NULL DEFAULT 0,
  `size_bytes` INTEGER NOT NULL DEFAULT 0,
  `ocr_language` VARCHAR(64) NULL,
  `engine_version` VARCHAR(64) NULL,
  `content_sha256` CHAR(64) NULL,
  `stored_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- 2. ocr_extractions_pages - one row per page, cascading with its extraction
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `ocr_extractions_pages` (
  `id` INTEGER PRIMARY KEY AUTOINCREMENT,
  `extraction_id` INTEGER NOT NULL,
  `page_number` INTEGER NOT NULL,
  `method` VARCHAR(16) NOT NULL DEFAULT 'ocr',
  `content` TEXT NOT NULL,
  `char_count` INTEGER NOT NULL DEFAULT 0,
  `word_count` INTEGER NOT NULL DEFAULT 0,
  `confidence` REAL NULL,
  `duration_ms` INTEGER NOT NULL DEFAULT 0,
  CONSTRAINT `uq_ocr_extractions_pages_page` UNIQUE (`extraction_id`, `page_number`),
  FOREIGN KEY (`extraction_id`) REFERENCES `ocr_extractions` (`id`) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- 3. Indexes - the same three the application creates (records view, search)
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS `idx_ocr_extractions_uploaded_at` ON `ocr_extractions` (`uploaded_at`);
CREATE INDEX IF NOT EXISTS `idx_ocr_extractions_filename` ON `ocr_extractions` (`filename`);
CREATE INDEX IF NOT EXISTS `idx_ocr_extractions_sha256` ON `ocr_extractions` (`content_sha256`);

-- ---------------------------------------------------------------------------
-- 4. Seed data (optional) - two extractions that match the files in samples/
--    Ids 9001+ so they cannot collide with a real upload; INSERT OR REPLACE
--    keeps the script idempotent.  The content is exactly what the application
--    extracted from
--      samples/images/scan_invoice.png          (1 page, OCR, 95.11 % confidence)
--      samples/pdf/scanned_invoice_3_pages.pdf  (3 pages, OCR, 95.47 %)
--    and `content_sha256` is the SHA-256 the application computed for it.  Stock
--    SQLite has no SHA2() function, so the digests are written out literally.
-- ---------------------------------------------------------------------------
INSERT OR REPLACE INTO `ocr_extractions`
  (`id`, `filename`, `uploaded_at`, `content`, `kind`, `page_count`, `char_count`,
   `word_count`, `confidence`, `duration_ms`, `size_bytes`, `ocr_language`,
   `engine_version`, `content_sha256`)
VALUES
  (9001, 'scan_invoice.png', '2026-09-29 21:29:33.940178',
   'ACME invoice 2026
Invoice no: 10042
Total: 128.50 EUR',
   'image', 1, 53, 9, 95.11, 218, 23292, 'eng', '5.4.0.20240606',
   '6bda0ca88e801d3a25004efb28e486631de8b79b19ae44e15d8904547b20fc2c'),
  (9002, 'scanned_invoice_3_pages.pdf', '2026-09-29 21:29:35.350399',
   '----- Page 1 of 3 -----

ACME purchase order ALPHA
Line one of the scanned document

----- Page 2 of 3 -----

ACME purchase order BRAVO
Line two of the scanned document

----- Page 3 of 3 -----

ACME purchase order CHARLIE
Line three of the scanned document',
   'pdf', 3, 178, 30, 95.47, 983, 76897, 'eng', '5.4.0.20240606',
   'dfdae19e1e5f5afdc17f7c2620a29f17a36c2223c58453e6a90abc47c3a69724');

INSERT OR REPLACE INTO `ocr_extractions_pages`
  (`id`, `extraction_id`, `page_number`, `method`, `content`, `char_count`,
   `word_count`, `confidence`, `duration_ms`)
VALUES
  (9001, 9001, 1, 'ocr',
   'ACME invoice 2026
Invoice no: 10042
Total: 128.50 EUR', 53, 9, 95.11, 191),
  (9002, 9002, 1, 'ocr',
   'ACME purchase order ALPHA
Line one of the scanned document', 58, 10, 95.40, 265),
  (9003, 9002, 2, 'ocr',
   'ACME purchase order BRAVO
Line two of the scanned document', 58, 10, 95.40, 251),
  (9004, 9002, 3, 'ocr',
   'ACME purchase order CHARLIE
Line three of the scanned document', 62, 10, 95.60, 255);

-- ---------------------------------------------------------------------------
-- 5. Verify
-- ---------------------------------------------------------------------------
-- SELECT `id`, `filename`, `kind`, `page_count`, `char_count`, `confidence`,
--        `content_sha256`, SUBSTR(`content`, 1, 40) AS `snippet` FROM `ocr_extractions`;
-- SELECT `extraction_id`, `page_number`, `method`, `char_count` FROM `ocr_extractions_pages`;
--
-- To remove the seed rows again (their page rows cascade):
--   DELETE FROM `ocr_extractions` WHERE `id` IN (9001, 9002);

