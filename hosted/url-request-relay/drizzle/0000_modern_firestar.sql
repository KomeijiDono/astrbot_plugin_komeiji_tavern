CREATE TABLE `temporary_requests` (
	`request_id` text PRIMARY KEY NOT NULL,
	`read_token_hash` text NOT NULL,
	`messages_json` text NOT NULL,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL,
	`expires_at` integer NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `temporary_requests_read_token_hash_unique` ON `temporary_requests` (`read_token_hash`);--> statement-breakpoint
CREATE INDEX `temporary_requests_expires_at_idx` ON `temporary_requests` (`expires_at`);