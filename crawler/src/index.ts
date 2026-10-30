import { type CrawlEnv, type QueueMessage, runQueue, runScheduled } from "./crawl";
import { fromD1 } from "./store";

interface WorkerEnv {
  DB: D1Database;
  CRAWL_QUEUE: Queue;
  YOUTUBE_API_KEY?: string;
  CRAWL_ENQUEUE_LIMIT?: string;
  CRAWL_STALE_SECONDS?: string;
  RELATED_AUTOTRACK_CAP?: string;
}

function crawlEnv(env: WorkerEnv): CrawlEnv {
  return {
    DB: fromD1(env.DB),
    CRAWL_QUEUE: env.CRAWL_QUEUE,
    YOUTUBE_API_KEY: env.YOUTUBE_API_KEY,
    CRAWL_ENQUEUE_LIMIT: env.CRAWL_ENQUEUE_LIMIT,
    CRAWL_STALE_SECONDS: env.CRAWL_STALE_SECONDS,
    RELATED_AUTOTRACK_CAP: env.RELATED_AUTOTRACK_CAP,
  };
}

export default {
  async scheduled(_controller: ScheduledController, env: WorkerEnv): Promise<void> {
    const result = await runScheduled(crawlEnv(env));
    console.log(JSON.stringify({ message: "scheduled end", ...result }));
  },
  async queue(batch: MessageBatch, env: WorkerEnv): Promise<void> {
    const messages: QueueMessage[] = batch.messages.map((message) => ({
      body: message.body,
      ack: () => message.ack(),
      retry: (options) => message.retry(options),
    }));
    const result = await runQueue(messages, crawlEnv(env));
    console.log(JSON.stringify({ message: "queue end", ...result }));
  },
};
