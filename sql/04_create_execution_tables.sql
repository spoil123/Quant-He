-- ============================================================
-- 04_create_execution_tables.sql
-- 第 5 层：模拟盘执行层（P1-11，2026-08-31 新增）
--
-- 目标：接 QMT(MiniQMT/xtquant) 模拟盘，拿「委托价 vs 成交价」的
-- 真实成交回报，回归 impact_coef —— 让成本系数从经验值变成实证值。
--
-- 设计：
--  1) execution_order  委托单（下单意图 + 券商回报状态）
--  2) execution_trade  成交回报（每笔成交一行，含委托价/成交价/数量）
--  3) impact_calib     每次回归的结论快照（回写 costs.yaml 前先留档）
-- ============================================================

USE `quant`;

SET NAMES utf8mb4;

-- ----------------------------------------------------------------
-- 1. 委托单（order）
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `execution_order` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `broker`        VARCHAR(16)  NOT NULL DEFAULT 'qmt'  COMMENT '券商通道: qmt/easytrader/paper',
  `ts_code`       VARCHAR(10)  NOT NULL                COMMENT '6位代码',
  `side`          ENUM('buy','sell') NOT NULL          COMMENT '买卖方向',
  `order_type`    ENUM('limit','market') NOT NULL DEFAULT 'limit' COMMENT '委托类型',
  `price`         DECIMAL(12,4)  DEFAULT NULL          COMMENT '委托价（限价单）；市价单为下单时参考价',
  `volume`        INT UNSIGNED  NOT NULL               COMMENT '委托数量（股）',
  `status`        VARCHAR(20)  NOT NULL DEFAULT 'submitted'
                  COMMENT 'submitted=已报 / filled=全部成交 / partial=部分成交 / canceled=已撤 / rejected=废单',
  `xt_order_id`   VARCHAR(32)  DEFAULT NULL            COMMENT '券商端委托号',
  `strategy_tag`  VARCHAR(64)  DEFAULT NULL            COMMENT '来源策略/调仓批次标记',
  `created_at`    DATETIME     NOT NULL                COMMENT '下单时间',
  `updated_at`    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_code_time` (`ts_code`, `created_at`),
  KEY `idx_status` (`status`),
  KEY `idx_tag` (`strategy_tag`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模拟盘委托单';


-- ----------------------------------------------------------------
-- 2. 成交回报（trade）—— impact 回归的原材料
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `execution_trade` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `order_id`      BIGINT UNSIGNED NOT NULL              COMMENT '关联 execution_order.id',
  `broker`        VARCHAR(16)  NOT NULL DEFAULT 'qmt',
  `ts_code`       VARCHAR(10)  NOT NULL,
  `side`          ENUM('buy','sell') NOT NULL,
  `order_price`   DECIMAL(12,4)  DEFAULT NULL           COMMENT '委托价',
  `deal_price`    DECIMAL(12,4)  NOT NULL               COMMENT '实际成交价（冲击成本的来源）',
  `volume`        INT UNSIGNED  NOT NULL                COMMENT '成交数量（股）',
  `deal_amount`   DECIMAL(20,2)  DEFAULT NULL           COMMENT '成交金额 = deal_price × volume',
  `deal_time`     DATETIME     NOT NULL                 COMMENT '成交时间',
  `created_at`    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_order` (`order_id`),
  KEY `idx_code_time` (`ts_code`, `deal_time`),
  CONSTRAINT `fk_trade_order` FOREIGN KEY (`order_id`) REFERENCES `execution_order` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模拟盘成交回报';


-- ----------------------------------------------------------------
-- 3. impact 回归结论快照（每次校准留档）
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `impact_calib` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `calib_date`    DATE         NOT NULL                COMMENT '校准日期',
  `n_orders`      INT          NOT NULL                COMMENT '样本委托数',
  `n_trades`      INT          NOT NULL                COMMENT '样本成交数',
  `period_start`  DATE         DEFAULT NULL            COMMENT '样本区间起点',
  `period_end`    DATE         DEFAULT NULL            COMMENT '样本区间终点',
  `base_bps`      DECIMAL(12,6) DEFAULT NULL           COMMENT '回归出的基础滑点 bps',
  `impact_coef`   DECIMAL(12,6) DEFAULT NULL           COMMENT '回归出的冲击系数',
  `r_squared`     DECIMAL(8,6)  DEFAULT NULL           COMMENT '拟合优度',
  `method`        VARCHAR(32)  DEFAULT NULL            COMMENT '回归方法: ols/quantile/robust',
  `note`          VARCHAR(255) DEFAULT NULL            COMMENT '备注（是否已回写 costs.yaml 等）',
  `created_at`    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_calib_date` (`calib_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='impact 系数校准快照';
