-- ===========================================================================
-- Flask OCR - MySQL schema + seed data
-- ===========================================================================
--
-- The application creates exactly this schema itself when you connect from the
-- /database page ("Connect & create schema", or POST /api/database/connect):
-- app/database.py builds this DDL with CREATE DATABASE/CREATE TABLE IF NOT
-- EXISTS.  This file is the same DDL as a plain script, for reviewers who want
-- to pre-create the objects, inspect them in SQL, or create them by hand.
--
-- Usage:
--   mysql -u root -p < sql/mysql_schema.sql
--   mysql -u root -p flask_ocr < sql/mysql_schema.sql      (schema exists)
--
-- or paste it into MySQL Workbench / phpMyAdmin / DBeaver and run it.
--
-- Naming convention (see PAGES_TABLE_SUFFIX in app/database.py): the parent
-- table is MYSQL_TABLE (default `ocr_extractions`) and the per-page table is
-- that name plus `_pages` (default `ocr_extractions_pages`).  Every statement
-- is idempotent, so running the script twice is safe.
--
-- Verified with MySQL 8.0 syntax; InnoDB + utf8mb4 only, no server specific
-- extensions.  Timestamps are stored in UTC (the application always writes UTC).
-- ===========================================================================

CREATE DATABASE IF NOT EXISTS `flask_ocr` CHARACTER SET utf8mb4;
USE `flask_ocr`;

-- ---------------------------------------------------------------------------
-- 1. ocr_extractions - one row per processed upload
-- ---------------------------------------------------------------------------
-- `supplier`, `invoice_number`, `document_date`, `total_amount` and `currency`
-- are the structured fields (app/fields.py): the reviewed supplier, invoice
-- number, document date, total amount and its currency.  NULL = not read / not
-- given.  They sit between `engine_version` and `content_sha256`, exactly where
-- the application's own DDL puts them.
CREATE TABLE IF NOT EXISTS `ocr_extractions` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `filename` VARCHAR(255) NOT NULL,
  `uploaded_at` DATETIME(6) NOT NULL,
  `content` LONGTEXT NOT NULL,
  `kind` VARCHAR(16) NOT NULL DEFAULT 'image',
  `page_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `char_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `word_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `confidence` DECIMAL(5,2) NULL,
  `duration_ms` INT UNSIGNED NOT NULL DEFAULT 0,
  `size_bytes` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `ocr_language` VARCHAR(64) NULL,
  `engine_version` VARCHAR(64) NULL,
  `supplier` VARCHAR(255) NULL,
  `invoice_number` VARCHAR(64) NULL,
  `document_date` DATE NULL,
  `total_amount` DECIMAL(12,2) NULL,
  `currency` CHAR(3) NULL,
  `content_sha256` CHAR(64) NULL,
  `stored_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ocr_extractions_uploaded_at` (`uploaded_at`),
  KEY `idx_ocr_extractions_filename` (`filename`),
  KEY `idx_ocr_extractions_sha256` (`content_sha256`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- A database created by an earlier version of this project has the same table
-- without the five structured field columns.  Connecting from /database (or
-- POST /database/schema) adds them automatically - MySQL has no
-- "ADD COLUMN IF NOT EXISTS", so these are the statements it runs, and they are
-- only needed once:
--
-- ALTER TABLE `ocr_extractions` ADD COLUMN `supplier` VARCHAR(255) NULL;
-- ALTER TABLE `ocr_extractions` ADD COLUMN `invoice_number` VARCHAR(64) NULL;
-- ALTER TABLE `ocr_extractions` ADD COLUMN `document_date` DATE NULL;
-- ALTER TABLE `ocr_extractions` ADD COLUMN `total_amount` DECIMAL(12,2) NULL;
-- ALTER TABLE `ocr_extractions` ADD COLUMN `currency` CHAR(3) NULL;


-- ---------------------------------------------------------------------------
-- 2. ocr_extractions_pages - one row per page, cascading with its extraction
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `ocr_extractions_pages` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `extraction_id` BIGINT UNSIGNED NOT NULL,
  `page_number` INT UNSIGNED NOT NULL,
  `method` VARCHAR(16) NOT NULL DEFAULT 'ocr',
  `content` LONGTEXT NOT NULL,
  `char_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `word_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `confidence` DECIMAL(5,2) NULL,
  `duration_ms` INT UNSIGNED NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ocr_extractions_pages_page` (`extraction_id`, `page_number`),
  CONSTRAINT `fk_ocr_extractions_pages_extraction` FOREIGN KEY (`extraction_id`)
    REFERENCES `ocr_extractions` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 3. Seed data (optional) - two extractions that match the files in samples/
--    They use ids 9001+ so they can never collide with a real upload, and the
--    first DELETE makes re-running the script idempotent (the page rows go with
--    their parent through ON DELETE CASCADE).
--
--    The content is exactly what the application extracted from
--      samples/images/scan_invoice.png          (1 page, OCR, 95.11 % confidence)
--      samples/pdf/scanned_invoice_3_pages.pdf  (3 pages, OCR, 95.47 %)
--    so the records view, the search and the .xlsx export can be tried without
--    uploading anything first.
-- ---------------------------------------------------------------------------
DELETE FROM `ocr_extractions` WHERE `id` IN (9001, 9002);

INSERT INTO `ocr_extractions`
  (`id`, `filename`, `uploaded_at`, `content`, `kind`, `page_count`, `char_count`,
   `word_count`, `confidence`, `duration_ms`, `size_bytes`, `ocr_language`,
   `engine_version`, `content_sha256`)
VALUES
  (9001, 'scan_invoice.png', '2026-09-29 21:29:33.940178',
   'ACME invoice 2026
Invoice no: 10042
Total: 128.50 EUR',
   'image', 1, 53, 9, 95.11, 218, 23292, 'eng', '5.4.0.20240606', NULL),
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
   'pdf', 3, 178, 30, 95.47, 983, 76897, 'eng', '5.4.0.20240606', NULL);

-- The structured fields those two documents produce (app/fields.py).  Scanning a
-- document proposes values with a confidence; the review page is where a human
-- confirms or corrects them, and these rows hold what was confirmed.  The 3 page
-- purchase order has no label the parser trusts, so it keeps only the supplier it
-- guessed from the header line.
UPDATE `ocr_extractions`
   SET `supplier` = 'ACME',
       `invoice_number` = '10042',
       `document_date` = NULL,
       `total_amount` = 128.50,
       `currency` = 'EUR'
 WHERE `id` = 9001;
UPDATE `ocr_extractions` SET `supplier` = 'ACME' WHERE `id` = 9002;


-- The application fills `content_sha256` on every upload; do the same here
-- (SHA2 is part of MySQL, so the digests are real, not invented).
UPDATE `ocr_extractions`
   SET `content_sha256` = SHA2(`content`, 256)
 WHERE `id` IN (9001, 9002) AND `content_sha256` IS NULL;

INSERT INTO `ocr_extractions_pages`
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
-- 4. Verify
-- ---------------------------------------------------------------------------
-- SELECT `id`, `filename`, `kind`, `page_count`, `char_count`, `confidence`,
--        `content_sha256`, LEFT(`content`, 40) AS `snippet` FROM `ocr_extractions`;
-- SELECT `extraction_id`, `page_number`, `method`, `char_count` FROM `ocr_extractions_pages`;
--
-- A user that may not CREATE the schema needs these rights instead:
--   GRANT SELECT, INSERT, UPDATE, DELETE ON `flask_ocr`.* TO 'ocr'@'%';
-- (CREATE is only required because connecting creates the schema and tables.)

