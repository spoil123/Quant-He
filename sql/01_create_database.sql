-- ============================================================
-- 01_create_database.sql
-- 创建数据库 quant
-- 执行：mysql -u root -p < sql/01_create_database.sql
-- 或：python scripts/init_db.py
-- ============================================================

CREATE DATABASE IF NOT EXISTS `quant`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE `quant`;

-- 说明：
-- - utf8mb4 支持完整的中文股票名称与行业名
-- - 所有表统一 InnoDB，支持事务与行级锁（批量写入时必需）
