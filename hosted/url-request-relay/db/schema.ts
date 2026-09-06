import { index, integer, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const temporaryRequests = sqliteTable(
  "temporary_requests",
  {
    requestId: text("request_id").primaryKey(),
    readTokenHash: text("read_token_hash").notNull().unique(),
    messagesJson: text("messages_json").notNull(),
    createdAt: integer("created_at").notNull(),
    updatedAt: integer("updated_at").notNull(),
    expiresAt: integer("expires_at").notNull(),
  },
  (table) => [index("temporary_requests_expires_at_idx").on(table.expiresAt)],
);
