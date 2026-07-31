/**
 * Polymarket Smart Money Tracker · BullMQ + Redis scheduler.
 *
 * Only the orchestration layer lives in Node; every actual collector call is
 * delegated to the Python pipeline (`python -m smart_money run ...`). Pino is
 * used for structured logs, BullMQ (backed by Redis) holds the schedule.
 *
 * Run:
 *   REDIS_URL=redis://127.0.0.1:6379 node scheduler/index.js
 */

const pino = require("pino");
const { Queue, Worker, QueueEvents } = require("bullmq");
const IORedis = require("ioredis");
const { spawn } = require("child_process");

const logger = pino({
  name: "smart-money-scheduler",
  level: process.env.LOG_LEVEL || "info",
});

const REDIS_URL = process.env.REDIS_URL || "redis://127.0.0.1:6379";
const QUEUE_NAME = "smart-money-collector";
const PYTHON_BIN = process.env.SMART_MONEY_PYTHON || "python";

const connection = new IORedis(REDIS_URL, { maxRetriesPerRequest: null });
const queue = new Queue(QUEUE_NAME, { connection });
const queueEvents = new QueueEvents(QUEUE_NAME, { connection });

const REPEATABLE_JOBS = [
  { name: "leaderboard", every: 24 * 60 * 60 * 1000, opts: { walletLimit: null } },
  { name: "markets", every: 6 * 60 * 60 * 1000, opts: { walletLimit: null } },
  { name: "trades", every: 5 * 60 * 1000, opts: { walletLimit: null } },
  { name: "positions", every: 5 * 60 * 1000, opts: { walletLimit: null } },
];

async function registerRepeatables() {
  for (const job of REPEATABLE_JOBS) {
    const key = `${QUEUE_NAME}:${job.name}`;
    await queue.add(
      job.name,
      { jobName: job.name, walletLimit: job.opts.walletLimit },
      {
        repeat: { every: job.every, key },
        removeOnComplete: 20,
        removeOnFail: 20,
      },
    );
    logger.info({ job: job.name, everyMs: job.every }, "scheduled repeatable job");
  }
}

function runPythonJob(jobName, walletLimit) {
  return new Promise((resolve) => {
    const args = ["-m", "smart_money", "run", "--job", jobName];
    if (walletLimit) args.push("--wallet-limit", String(walletLimit));
    logger.info({ job: jobName, args }, "spawning python pipeline");
    const child = spawn(PYTHON_BIN, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("exit", (code) => {
      if (code === 0) {
        logger.info({ job: jobName }, "python pipeline finished");
        resolve({ ok: true, stdout, stderr });
      } else {
        logger.error({ job: jobName, code, stderr }, "python pipeline failed");
        resolve({ ok: false, code, stdout, stderr });
      }
    });
  });
}

async function startWorker() {
  const worker = new Worker(
    QUEUE_NAME,
    async (job) => runPythonJob(job.data.jobName, job.data.walletLimit),
    { connection, concurrency: 1 },
  );
  worker.on("completed", (job) => logger.info({ jobId: job.id, name: job.name }, "job completed"));
  worker.on("failed", (job, err) => logger.error({ jobId: job?.id, err: err?.message }, "job failed"));
  return worker;
}

async function main() {
  await registerRepeatables();
  const worker = await startWorker();
  queueEvents.on("completed", ({ jobId }) => logger.debug({ jobId }, "queueEvents completed"));
  logger.info("smart-money scheduler started");
  process.on("SIGINT", async () => {
    logger.info("shutting down");
    await worker.close();
    await queue.close();
    await queueEvents.close();
    await connection.quit();
    process.exit(0);
  });
}

main().catch((err) => {
  logger.error({ err: err.message }, "scheduler crashed");
  process.exit(1);
});
