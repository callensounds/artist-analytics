interface D1Prepared {
  bind: (...params: unknown[]) => {
    all: () => Promise<{ results?: unknown[] }>;
    first: <T>() => Promise<T | null>;
    run: () => Promise<unknown>;
  };
}

interface D1Database {
  prepare: (sql: string) => D1Prepared;
  batch: (statements: unknown[]) => Promise<unknown>;
}

interface Queue {
  send: (body: unknown) => Promise<unknown>;
}

interface ScheduledController {
  cron: string;
}

interface MessageBatch {
  messages: { body: unknown; ack: () => void; retry: (options?: { delaySeconds?: number }) => void }[];
}
