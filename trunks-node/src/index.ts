import { exec, execFile } from "node:child_process";
import { Buffer } from "node:buffer";
import { mkdir } from "node:fs/promises";
import { Socket } from "node:net";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const PROTOCOL_VERSION = "2026-04-29";
const DEFAULT_RPC_TIMEOUT_MS = 30_000;

/**
 * Trunks application error codes. Mirrors `trunks/protocol/errors.py`.
 * Drift between the two files is caught by `tests/unit/test_rpc_protocol.py`.
 */
export const ErrorCode = {
  ParseError: -32700,
  InvalidRequest: -32600,
  MethodNotFound: -32601,
  InvalidParams: -32602,
  InternalError: -32603,
  ProtocolVersion: -32000,
  RepoNotFound: -32001,
  RefNotFound: -32002,
  BackendUnavailable: -32003,
  Conflict: -32004,
  PolicyDenied: -32005,
  JournalCorrupt: -32006,
  DaemonUnreachable: -32007,
  FileNotFound: -32008,
} as const;
export type ErrorCodeValue = (typeof ErrorCode)[keyof typeof ErrorCode];

export interface TrunksOptions {
  cwd?: string;
  bin?: string;
  socket?: string;
  rpcTimeoutMs?: number;
}

export interface MountOptions {
  repo: string;
  path?: string;
  backend?: string;
  mode?: "virtual" | "disk";
  watch?: boolean;
}

export interface CommandResult {
  stdout: string;
  stderr: string;
  exitCode: number;
}

export interface RuntimeStatus {
  running: boolean;
  pid: number | null;
  runtimePath: string;
  socketPath: string;
  protocolVersion: string;
}

export interface RepoStatus {
  repository: string;
  path: string;
  branch: string;
  head: string | null;
  backend: string | null;
  dirty: boolean;
}

export interface DiffEntry {
  status: "A" | "M" | "D";
  path: string;
}

export interface CheckpointOptions {
  message?: string;
}

export interface DiffOptions {
  vs?: string;
  from?: string;
}

export interface RollbackOptions {
  to: string;
}

export interface BranchCreateOptions {
  name: string;
  from?: string;
}

export interface BranchOptions {
  name: string;
}

export interface RepoCreateOptions {
  name: string;
  path?: string;
  backend?: string;
}

export interface RepoGetOptions {
  name?: string;
  path?: string;
}

export interface RepoUpdateOptions {
  name?: string;
  path?: string;
  backend: string;
}

export interface RepoDeleteOptions {
  name?: string;
  path?: string;
}

export interface BranchUpdateOptions {
  name: string;
  from: string;
}

export interface TagCreateOptions {
  name: string;
  at?: string;
}

export interface TagOptions {
  name: string;
}

export interface TagUpdateOptions {
  name: string;
  at: string;
}

export interface StorageCreateOptions {
  name: string;
  url: string;
  mirror?: boolean;
}

export interface StorageOptions {
  name: string;
}

export interface WebhookCreateOptions {
  url: string;
  events?: string[];
}

export interface WebhookGetOptions {
  id: string;
}

export type WebhookDeleteOptions = WebhookGetOptions;

export interface WebhookUpdateOptions {
  id: string;
  url?: string;
  events?: string[];
}

export interface LogOptions {
  limit?: number;
}

export interface ListOptions {
  limit?: number;
  offset?: number;
}

export interface ListResponse<T> {
  object: "list";
  data: T[];
  limit: number;
  offset: number;
  total_count: number;
  has_more: boolean;
}

export interface RepoResource {
  object: "repo";
  id: string;
  name: string;
  path: string;
  repo_data_path: string;
  current_branch: string;
  head: string | null;
  backend: string | null;
  storage: StorageTargetResource[];
}

export interface BranchResource {
  object: "branch";
  id: string;
  name: string;
  ref: string;
  head: string;
  current: boolean;
}

export interface TagResource {
  object: "tag";
  id: string;
  name: string;
  ref: string;
  target: string;
}

export interface DeletedResource {
  object: string;
  id: string;
  name?: string;
  deleted: true;
}

export interface StorageTargetResource {
  object: "storage_target";
  id: string;
  name: string;
  role?: string;
  backend?: string;
  url?: string;
}

export interface WebhookResource {
  object: "webhook";
  id: string;
  url: string;
  events: string[];
}

export interface AuditEventResource {
  object: "audit_event";
  id: string;
  ts: number;
  event: string;
  data: Record<string, unknown>;
}

export interface CommitSummary {
  id: string;
  tree: string;
  parents: string[];
  message: string;
  author: { name: string; email: string; when: string };
}

export class TrunksRpcError extends Error {
  constructor(
    public readonly code: number,
    message: string,
    public readonly data?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "TrunksRpcError";
  }
}

export class ProtocolVersionError extends TrunksRpcError {
  constructor(message: string, data?: Record<string, unknown>) {
    super(ErrorCode.ProtocolVersion, message, data);
    this.name = "ProtocolVersionError";
  }
}

export class RepoNotFoundError extends TrunksRpcError {
  constructor(message: string, data?: Record<string, unknown>) {
    super(ErrorCode.RepoNotFound, message, data);
    this.name = "RepoNotFoundError";
  }
}

export class RefNotFoundError extends TrunksRpcError {
  constructor(message: string, data?: Record<string, unknown>) {
    super(ErrorCode.RefNotFound, message, data);
    this.name = "RefNotFoundError";
  }
}

export class BackendUnavailableError extends TrunksRpcError {
  constructor(message: string, data?: Record<string, unknown>) {
    super(ErrorCode.BackendUnavailable, message, data);
    this.name = "BackendUnavailableError";
  }
}

export class ConflictError extends TrunksRpcError {
  constructor(message: string, data?: Record<string, unknown>) {
    super(ErrorCode.Conflict, message, data);
    this.name = "ConflictError";
  }
}

export class PolicyDeniedError extends TrunksRpcError {
  constructor(message: string, data?: Record<string, unknown>) {
    super(ErrorCode.PolicyDenied, message, data);
    this.name = "PolicyDeniedError";
  }
}

export class JournalCorruptError extends TrunksRpcError {
  constructor(message: string, data?: Record<string, unknown>) {
    super(ErrorCode.JournalCorrupt, message, data);
    this.name = "JournalCorruptError";
  }
}

export class DaemonUnreachableError extends TrunksRpcError {
  constructor(socketPath: string, reason = "not reachable") {
    super(ErrorCode.DaemonUnreachable, `Trunks daemon is ${reason} at ${socketPath}`, { socketPath });
    this.name = "DaemonUnreachableError";
  }
}

export class FileNotFoundError extends TrunksRpcError {
  constructor(message: string, data?: Record<string, unknown>) {
    super(ErrorCode.FileNotFound, message, data);
    this.name = "FileNotFoundError";
  }
}

function buildRpcError(code: number, message: string, data?: Record<string, unknown>): TrunksRpcError {
  switch (code) {
    case ErrorCode.ProtocolVersion:
      return new ProtocolVersionError(message, data);
    case ErrorCode.RepoNotFound:
      return new RepoNotFoundError(message, data);
    case ErrorCode.RefNotFound:
      return new RefNotFoundError(message, data);
    case ErrorCode.BackendUnavailable:
      return new BackendUnavailableError(message, data);
    case ErrorCode.Conflict:
      return new ConflictError(message, data);
    case ErrorCode.PolicyDenied:
      return new PolicyDeniedError(message, data);
    case ErrorCode.JournalCorrupt:
      return new JournalCorruptError(message, data);
    case ErrorCode.FileNotFound:
      return new FileNotFoundError(message, data);
    default:
      return new TrunksRpcError(code, message, data);
  }
}

export class Trunks {
  private readonly cwd: string;
  private readonly bin: string;
  private readonly socket?: string;
  private readonly rpcTimeoutMs: number;
  readonly repos: TrunksRepos;
  readonly branches: TrunksBranches;
  readonly tags: TrunksTags;
  readonly storage: TrunksStorage;
  readonly webhooks: TrunksWebhooks;
  readonly audit: TrunksAudit;

  constructor(options: TrunksOptions = {}) {
    this.cwd = options.cwd ?? process.cwd();
    this.bin = options.bin ?? "trunks";
    this.socket = options.socket;
    this.rpcTimeoutMs = options.rpcTimeoutMs ?? DEFAULT_RPC_TIMEOUT_MS;
    this.repos = new TrunksRepos(this);
    this.branches = new TrunksBranches(this);
    this.tags = new TrunksTags(this);
    this.storage = new TrunksStorage(this);
    this.webhooks = new TrunksWebhooks(this);
    this.audit = new TrunksAudit(this);
  }

  async mount(options: MountOptions): Promise<TrunksFileSystem> {
    const args = ["mount", "--repo", options.repo];
    if (options.path) args.push("--path", options.path);
    if (options.backend) args.push("--backend", options.backend);
    if (options.mode === "virtual") args.push("--mode", "virtual");
    if (options.mode === "disk" || options.watch === true) args.push("--mode", "disk");
    if (options.watch === true) args.push("--watch");
    await assertOk(await this.cli(args), "mount");
    const root = options.path ?? `${this.cwd}/${options.repo}`;
    const fs = this.fs(root);
    if (options.mode === "virtual" || options.watch === true) await fs.handshake();
    return fs;
  }

  fs(path: string = this.cwd): TrunksFileSystem {
    return new TrunksFileSystem({
      cwd: path,
      rpc: new JsonRpcClient(this.socket ?? `${path}/.trunks/runtime/daemon.sock`, this.rpcTimeoutMs),
      cli: this,
    });
  }

  async status(path: string = this.cwd): Promise<RepoStatus> {
    return this.fs(path).status();
  }

  async storageWizard(): Promise<CommandResult> {
    return this.cli(["storage", "wizard"]);
  }

  async checkpoint(message: string): Promise<CommandResult> {
    return assertOk(await this.cli(["checkpoint", "-m", message]), "checkpoint");
  }

  async diff(options: DiffOptions = {}): Promise<DiffEntry[]> {
    const args = ["diff", "--json"];
    if (options.vs) args.push("--vs", options.vs);
    if (options.from) args.push("--from", options.from);
    const result = await this.cli(args);
    if (result.exitCode !== 0) {
      throw new TrunksRpcError(ErrorCode.InternalError, `diff failed: ${result.stderr.trim() || result.stdout.trim()}`);
    }
    const parsed: unknown = JSON.parse(result.stdout || "[]");
    if (!Array.isArray(parsed)) {
      throw new TrunksRpcError(ErrorCode.InternalError, "diff did not return an array");
    }
    return parsed.map(assertDiffEntry);
  }

  async rollback(options: RollbackOptions): Promise<CommandResult> {
    return assertOk(await this.cli(["rollback", "--to", options.to]), "rollback");
  }

  async push(): Promise<CommandResult> {
    return assertOk(await this.cli(["push"]), "push");
  }

  async pull(): Promise<CommandResult> {
    return assertOk(await this.cli(["pull"]), "pull");
  }

  async fetch(): Promise<CommandResult> {
    return assertOk(await this.cli(["fetch"]), "fetch");
  }

  async log(options: LogOptions = {}): Promise<CommitSummary[]> {
    const result = await this.cli(["log", "--json"]);
    if (result.exitCode !== 0) {
      throw new TrunksRpcError(ErrorCode.InternalError, `log failed: ${result.stderr.trim() || result.stdout.trim()}`);
    }
    const lines = result.stdout
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    const commits = lines.map((line) => JSON.parse(line) as CommitSummary);
    return options.limit !== undefined ? commits.slice(0, options.limit) : commits;
  }

  async history(): Promise<unknown> {
    const result = await this.cli(["history", "--json"]);
    if (result.exitCode !== 0) {
      throw new TrunksRpcError(ErrorCode.InternalError, `history failed: ${result.stderr.trim() || result.stdout.trim()}`);
    }
    return JSON.parse(result.stdout || "{}");
  }

  async cli(args: string[], cwd: string = this.cwd): Promise<CommandResult> {
    try {
      const { stdout, stderr } = await execFileAsync(this.bin, args, { cwd });
      return { stdout, stderr, exitCode: 0 };
    } catch (error) {
      if (isExecError(error)) {
        return { stdout: error.stdout ?? "", stderr: error.stderr ?? "", exitCode: error.code ?? 1 };
      }
      throw error;
    }
  }
}

export class TrunksRepos {
  constructor(private readonly trunks: Trunks) {}

  async create(options: RepoCreateOptions): Promise<RepoResource> {
    const args = ["repo", "create", "--name", options.name, "--json"];
    if (options.path) args.push("--path", options.path);
    if (options.backend) args.push("--backend", options.backend);
    const result = await assertOk(await this.trunks.cli(args), "repo create");
    return assertRepoResource(JSON.parse(result.stdout || "{}"));
  }

  async get(options: RepoGetOptions = {}): Promise<RepoResource> {
    const args = ["repo", "get", "--json"];
    if (options.name) args.push("--name", options.name);
    if (options.path) args.push("--path", options.path);
    const result = await assertOk(await this.trunks.cli(args), "repo get");
    return assertRepoResource(JSON.parse(result.stdout || "{}"));
  }

  async update(options: RepoUpdateOptions): Promise<RepoResource> {
    const args = ["repo", "update", "--backend", options.backend, "--json"];
    if (options.name) args.push("--name", options.name);
    if (options.path) args.push("--path", options.path);
    const result = await assertOk(await this.trunks.cli(args), "repo update");
    return assertRepoResource(JSON.parse(result.stdout || "{}"));
  }

  async delete(options: RepoDeleteOptions = {}): Promise<DeletedResource> {
    const args = ["repo", "delete", "--json"];
    if (options.name) args.push("--name", options.name);
    if (options.path) args.push("--path", options.path);
    const result = await assertOk(await this.trunks.cli(args), "repo delete");
    return assertDeletedResource(JSON.parse(result.stdout || "{}"), "repo");
  }

  async list(options: ListOptions = {}): Promise<ListResponse<RepoResource>> {
    const result = await this.trunks.cli(apiListArgs(["repo", "list"], options));
    if (result.exitCode !== 0) {
      throw new TrunksRpcError(ErrorCode.InternalError, `repo list failed: ${result.stderr.trim() || result.stdout.trim()}`);
    }
    return assertListResponse(result.stdout, assertRepoResource, "repo");
  }
}

export class TrunksBranches {
  constructor(private readonly trunks: Trunks) {}

  async create(options: BranchCreateOptions): Promise<BranchResource> {
    const args = ["branch", "create", "--name", options.name, "--json"];
    if (options.from) args.push("--from", options.from);
    const result = await assertOk(await this.trunks.cli(args), "branch create");
    return assertBranchResource(JSON.parse(result.stdout || "{}"));
  }

  async get(options: BranchOptions): Promise<BranchResource> {
    const result = await assertOk(
      await this.trunks.cli(["branch", "get", "--name", options.name, "--json"]),
      "branch get",
    );
    return assertBranchResource(JSON.parse(result.stdout || "{}"));
  }

  async update(options: BranchUpdateOptions): Promise<BranchResource> {
    const result = await assertOk(
      await this.trunks.cli(["branch", "update", "--name", options.name, "--from", options.from, "--json"]),
      "branch update",
    );
    return assertBranchResource(JSON.parse(result.stdout || "{}"));
  }

  async switch(options: BranchOptions): Promise<BranchResource> {
    const result = await assertOk(
      await this.trunks.cli(["branch", "switch", "--name", options.name, "--json"]),
      "branch switch",
    );
    return assertBranchResource(JSON.parse(result.stdout || "{}"));
  }

  async delete(options: BranchOptions): Promise<DeletedResource> {
    const result = await assertOk(
      await this.trunks.cli(["branch", "delete", "--name", options.name, "--json"]),
      "branch delete",
    );
    return assertDeletedResource(JSON.parse(result.stdout || "{}"), "branch");
  }

  async list(options: ListOptions = {}): Promise<ListResponse<BranchResource>> {
    const result = await this.trunks.cli(apiListArgs(["branch", "list"], options));
    if (result.exitCode !== 0) {
      throw new TrunksRpcError(ErrorCode.InternalError, `branch list failed: ${result.stderr.trim() || result.stdout.trim()}`);
    }
    return assertListResponse(result.stdout, assertBranchResource, "branch");
  }
}

export class TrunksTags {
  constructor(private readonly trunks: Trunks) {}

  async create(options: TagCreateOptions): Promise<TagResource> {
    const args = ["tag", "create", "--name", options.name, "--json"];
    if (options.at) args.push("--at", options.at);
    const result = await assertOk(await this.trunks.cli(args), "tag create");
    return assertTagResource(JSON.parse(result.stdout || "{}"));
  }

  async get(options: TagOptions): Promise<TagResource> {
    const result = await assertOk(await this.trunks.cli(["tag", "get", "--name", options.name, "--json"]), "tag get");
    return assertTagResource(JSON.parse(result.stdout || "{}"));
  }

  async update(options: TagUpdateOptions): Promise<TagResource> {
    const result = await assertOk(
      await this.trunks.cli(["tag", "update", "--name", options.name, "--at", options.at, "--json"]),
      "tag update",
    );
    return assertTagResource(JSON.parse(result.stdout || "{}"));
  }

  async delete(options: TagOptions): Promise<DeletedResource> {
    const result = await assertOk(await this.trunks.cli(["tag", "delete", "--name", options.name, "--json"]), "tag delete");
    return assertDeletedResource(JSON.parse(result.stdout || "{}"), "tag");
  }

  async list(options: ListOptions = {}): Promise<ListResponse<TagResource>> {
    const result = await this.trunks.cli(apiListArgs(["tag", "list"], options));
    if (result.exitCode !== 0) {
      throw new TrunksRpcError(ErrorCode.InternalError, `tag list failed: ${result.stderr.trim() || result.stdout.trim()}`);
    }
    return assertListResponse(result.stdout, assertTagResource, "tag");
  }
}

export class TrunksStorage {
  constructor(private readonly trunks: Trunks) {}

  async create(options: StorageCreateOptions): Promise<StorageTargetResource> {
    const args = ["storage", "create", "--name", options.name, "--url", options.url, "--json"];
    if (options.mirror === true) args.push("--mirror");
    const result = await assertOk(await this.trunks.cli(args), "storage create");
    return assertStorageTargetResource(JSON.parse(result.stdout || "{}"));
  }

  async get(options: StorageOptions): Promise<StorageTargetResource> {
    const result = await assertOk(await this.trunks.cli(["storage", "get", "--name", options.name, "--json"]), "storage get");
    return assertStorageTargetResource(JSON.parse(result.stdout || "{}"));
  }

  async update(options: StorageCreateOptions): Promise<StorageTargetResource> {
    const args = ["storage", "update", "--name", options.name, "--url", options.url, "--json"];
    if (options.mirror === true) args.push("--mirror");
    const result = await assertOk(await this.trunks.cli(args), "storage update");
    return assertStorageTargetResource(JSON.parse(result.stdout || "{}"));
  }

  async delete(options: StorageOptions): Promise<DeletedResource> {
    const result = await assertOk(
      await this.trunks.cli(["storage", "delete", "--name", options.name, "--json"]),
      "storage delete",
    );
    return assertDeletedResource(JSON.parse(result.stdout || "{}"), "storage_target");
  }

  async list(options: ListOptions = {}): Promise<ListResponse<StorageTargetResource>> {
    const result = await this.trunks.cli(apiListArgs(["storage", "list"], options));
    if (result.exitCode !== 0) {
      throw new TrunksRpcError(ErrorCode.InternalError, `storage list failed: ${result.stderr.trim() || result.stdout.trim()}`);
    }
    return assertListResponse(result.stdout, assertStorageTargetResource, "storage");
  }
}

export class TrunksWebhooks {
  constructor(private readonly trunks: Trunks) {}

  async create(options: WebhookCreateOptions): Promise<WebhookResource> {
    const args = ["webhook", "create", options.url, "--json"];
    if (options.events?.length) args.push("--on", options.events.join(","));
    const result = await assertOk(await this.trunks.cli(args), "webhook add");
    return assertWebhookResource(JSON.parse(result.stdout || "{}"));
  }

  async get(options: WebhookGetOptions): Promise<WebhookResource> {
    const result = await assertOk(await this.trunks.cli(["webhook", "get", options.id, "--json"]), "webhook get");
    return assertWebhookResource(JSON.parse(result.stdout || "{}"));
  }

  async update(options: WebhookUpdateOptions): Promise<WebhookResource> {
    const args = ["webhook", "update", options.id, "--json"];
    if (options.url) args.push("--url", options.url);
    if (options.events?.length) args.push("--on", options.events.join(","));
    const result = await assertOk(await this.trunks.cli(args), "webhook update");
    return assertWebhookResource(JSON.parse(result.stdout || "{}"));
  }

  async list(options: ListOptions = {}): Promise<ListResponse<WebhookResource>> {
    const result = await this.trunks.cli(apiListArgs(["webhook", "list"], options));
    if (result.exitCode !== 0) {
      throw new TrunksRpcError(ErrorCode.InternalError, `webhook list failed: ${result.stderr.trim() || result.stdout.trim()}`);
    }
    return assertListResponse(result.stdout, assertWebhookResource, "webhook");
  }

  async delete(options: WebhookDeleteOptions): Promise<DeletedResource> {
    const result = await assertOk(await this.trunks.cli(["webhook", "delete", options.id, "--json"]), "webhook delete");
    return assertDeletedResource(JSON.parse(result.stdout || "{}"), "webhook");
  }
}

export class TrunksAudit {
  constructor(private readonly trunks: Trunks) {}

  async list(options: ListOptions = {}): Promise<ListResponse<AuditEventResource>> {
    const result = await this.trunks.cli(apiListArgs(["audit", "list"], options));
    if (result.exitCode !== 0) {
      throw new TrunksRpcError(ErrorCode.InternalError, `audit list failed: ${result.stderr.trim() || result.stdout.trim()}`);
    }
    return assertListResponse(result.stdout, assertAuditEventResource, "audit");
  }
}

function apiListArgs(base: string[], options: ListOptions): string[] {
  const args = [...base, "--json"];
  if (options.limit !== undefined) args.push("--limit", String(options.limit));
  if (options.offset !== undefined) args.push("--offset", String(options.offset));
  return args;
}

function assertListResponse<T>(
  output: string,
  itemGuard: (value: unknown) => T,
  label: string,
): ListResponse<T> {
  const parsed: unknown = JSON.parse(output || "{}");
  if (
    !isRecord(parsed) ||
    parsed.object !== "list" ||
    !Array.isArray(parsed.data) ||
    typeof parsed.limit !== "number" ||
    typeof parsed.offset !== "number" ||
    typeof parsed.total_count !== "number" ||
    typeof parsed.has_more !== "boolean"
  ) {
    throw new TrunksRpcError(ErrorCode.InternalError, `${label} list did not return a list response`);
  }
  return {
    object: "list",
    data: parsed.data.map(itemGuard),
    limit: parsed.limit,
    offset: parsed.offset,
    total_count: parsed.total_count,
    has_more: parsed.has_more,
  };
}

function assertRepoResource(value: unknown): RepoResource {
  if (
    !isRecord(value) ||
    value.object !== "repo" ||
    typeof value.id !== "string" ||
    typeof value.name !== "string" ||
    typeof value.path !== "string" ||
    typeof value.repo_data_path !== "string" ||
    typeof value.current_branch !== "string" ||
    !(typeof value.head === "string" || value.head === null) ||
    !(typeof value.backend === "string" || value.backend === null) ||
    !Array.isArray(value.storage)
  ) {
    throw new TrunksRpcError(ErrorCode.InternalError, "invalid repo resource");
  }
  value.storage.forEach(assertStorageTargetResource);
  return value as unknown as RepoResource;
}

function assertBranchResource(value: unknown): BranchResource {
  if (
    !isRecord(value) ||
    value.object !== "branch" ||
    typeof value.id !== "string" ||
    typeof value.name !== "string" ||
    typeof value.ref !== "string" ||
    typeof value.head !== "string" ||
    typeof value.current !== "boolean"
  ) {
    throw new TrunksRpcError(ErrorCode.InternalError, "invalid branch resource");
  }
  return value as unknown as BranchResource;
}

function assertTagResource(value: unknown): TagResource {
  if (
    !isRecord(value) ||
    value.object !== "tag" ||
    typeof value.id !== "string" ||
    typeof value.name !== "string" ||
    typeof value.ref !== "string" ||
    typeof value.target !== "string"
  ) {
    throw new TrunksRpcError(ErrorCode.InternalError, "invalid tag resource");
  }
  return value as unknown as TagResource;
}

function assertDeletedResource(value: unknown, object: string): DeletedResource {
  if (
    !isRecord(value) ||
    value.object !== object ||
    typeof value.id !== "string" ||
    !(typeof value.name === "string" || value.name === undefined) ||
    value.deleted !== true
  ) {
    throw new TrunksRpcError(ErrorCode.InternalError, `invalid deleted ${object} resource`);
  }
  return value as unknown as DeletedResource;
}

function assertStorageTargetResource(value: unknown): StorageTargetResource {
  if (
    !isRecord(value) ||
    value.object !== "storage_target" ||
    typeof value.id !== "string" ||
    typeof value.name !== "string" ||
    !(typeof value.role === "string" || value.role === undefined) ||
    !(typeof value.backend === "string" || value.backend === undefined) ||
    !(typeof value.url === "string" || value.url === undefined)
  ) {
    throw new TrunksRpcError(ErrorCode.InternalError, "invalid storage target resource");
  }
  return value as unknown as StorageTargetResource;
}

function assertWebhookResource(value: unknown): WebhookResource {
  if (
    !isRecord(value) ||
    value.object !== "webhook" ||
    typeof value.id !== "string" ||
    typeof value.url !== "string" ||
    !Array.isArray(value.events) ||
    !value.events.every((event) => typeof event === "string")
  ) {
    throw new TrunksRpcError(ErrorCode.InternalError, "invalid webhook resource");
  }
  return value as unknown as WebhookResource;
}

function assertAuditEventResource(value: unknown): AuditEventResource {
  if (
    !isRecord(value) ||
    value.object !== "audit_event" ||
    typeof value.id !== "string" ||
    typeof value.ts !== "number" ||
    typeof value.event !== "string" ||
    !isRecord(value.data)
  ) {
    throw new TrunksRpcError(ErrorCode.InternalError, "invalid audit event resource");
  }
  return value as unknown as AuditEventResource;
}

function assertOk(result: CommandResult, label: string): CommandResult {
  if (result.exitCode !== 0) {
    throw new TrunksRpcError(
      ErrorCode.InternalError,
      `${label} failed: ${result.stderr.trim() || result.stdout.trim()}`,
    );
  }
  return result;
}

export interface TrunksFileSystemOptions {
  cwd: string;
  rpc: JsonRpcClient;
  cli: Trunks;
}

export class TrunksFileSystem {
  private readonly cwd: string;
  private readonly rpcClient: JsonRpcClient;
  private readonly cliClient: Trunks;

  constructor(options: TrunksFileSystemOptions) {
    this.cwd = options.cwd;
    this.rpcClient = options.rpc;
    this.cliClient = options.cli;
  }

  async handshake(): Promise<RuntimeStatus> {
    const result = await this.rpcClient.call("runtime.status", {});
    return assertRuntimeStatus(result);
  }

  async status(): Promise<RepoStatus> {
    return assertRepoStatus(await this.rpcClient.call("repo.status", { compareWorktree: true }));
  }

  async read(path: string): Promise<Buffer> {
    const result = await this.rpcClient.call("fs.read", { path });
    if (!isRecord(result) || typeof result.dataBase64 !== "string") {
      throw new TrunksRpcError(ErrorCode.InternalError, "invalid fs.read response");
    }
    return Buffer.from(result.dataBase64, "base64");
  }

  async write(path: string, data: string | Uint8Array): Promise<void> {
    const payload = typeof data === "string" ? { path, text: data } : { path, dataBase64: Buffer.from(data).toString("base64") };
    await this.rpcClient.call("fs.write", payload);
  }

  async list(path: string = "."): Promise<string[]> {
    const result = await this.rpcClient.call("fs.list", { path });
    if (!Array.isArray(result) || !result.every((item) => typeof item === "string")) {
      throw new TrunksRpcError(ErrorCode.InternalError, "invalid fs.list response");
    }
    return result;
  }

  async exists(path: string): Promise<boolean> {
    const result = await this.rpcClient.call("fs.exists", { path });
    if (typeof result !== "boolean") throw new TrunksRpcError(ErrorCode.InternalError, "invalid fs.exists response");
    return result;
  }

  async remove(path: string): Promise<void> {
    await this.rpcClient.call("fs.remove", { path });
  }

  async copy(source: string, dest: string): Promise<void> {
    await this.rpcClient.call("fs.copy", { source, dest });
  }

  async move(source: string, dest: string): Promise<void> {
    await this.rpcClient.call("fs.move", { source, dest });
  }

  async mkdir(path: string): Promise<void> {
    await this.rpcClient.call("fs.mkdir", { path });
  }

  async checkpoint(message: string): Promise<unknown> {
    return this.rpcClient.call("vcs.checkpoint", { message });
  }

  async diff(vs = "main"): Promise<DiffEntry[]> {
    const result = await this.rpcClient.call("vcs.diff", { vs });
    if (!Array.isArray(result)) throw new TrunksRpcError(ErrorCode.InternalError, "invalid vcs.diff response");
    return result.map(assertDiffEntry);
  }

  async push(): Promise<unknown> {
    return this.rpcClient.call("vcs.push", {});
  }

  async pull(): Promise<unknown> {
    return this.rpcClient.call("vcs.pull", {});
  }

  localShell(): TrunksLocalShell {
    return new TrunksLocalShell(this.cwd);
  }
}

export class TrunksLocalShell {
  constructor(private readonly cwd: string) {}

  async exec(command: string): Promise<CommandResult> {
    await mkdir(this.cwd, { recursive: true });
    return new Promise((resolve) => {
      exec(command, { cwd: this.cwd }, (error, stdout, stderr) => {
        resolve({ stdout, stderr, exitCode: error && "code" in error && typeof error.code === "number" ? error.code : 0 });
      });
    });
  }
}

export class JsonRpcClient {
  private nextId = 1;

  constructor(
    private readonly socketPath: string,
    private readonly timeoutMs = DEFAULT_RPC_TIMEOUT_MS,
  ) {}

  async call(method: string, params: Record<string, unknown>): Promise<unknown> {
    const id = this.nextId++;
    const request = JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n";
    const response = await this.send(request);
    if (!isRecord(response)) throw new TrunksRpcError(ErrorCode.InternalError, "invalid JSON-RPC response");
    if ("error" in response) {
      const error = response.error;
      if (!isRecord(error) || typeof error.code !== "number" || typeof error.message !== "string") {
        throw new TrunksRpcError(ErrorCode.InternalError, "invalid JSON-RPC error");
      }
      throw buildRpcError(error.code, error.message, isRecord(error.data) ? error.data : undefined);
    }
    return response.result;
  }

  async handshake(): Promise<unknown> {
    return this.call("hello.handshake", { protocolVersion: PROTOCOL_VERSION });
  }

  private send(payload: string): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const socket = new Socket();
      let data = "";
      let settled = false;
      let timer: NodeJS.Timeout;
      const finish = (settle: () => void): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        socket.removeAllListeners();
        socket.destroy();
        settle();
      };
      timer = setTimeout(() => {
        finish(() => reject(new DaemonUnreachableError(this.socketPath, "timed out")));
      }, this.timeoutMs);

      socket.on("error", () => finish(() => reject(new DaemonUnreachableError(this.socketPath))));
      socket.on("data", (chunk: Buffer) => {
        data += chunk.toString("utf8");
        if (data.includes("\n")) socket.end();
      });
      socket.on("close", () => {
        const line = data.trim();
        if (!line) {
          finish(() => reject(new DaemonUnreachableError(this.socketPath, "closed without a response")));
          return;
        }
        try {
          const parsed: unknown = JSON.parse(line);
          finish(() => resolve(parsed));
        } catch (error) {
          finish(() => reject(new TrunksRpcError(ErrorCode.InternalError, "invalid JSON-RPC response", { socketPath: this.socketPath })));
        }
      });
      socket.connect(this.socketPath, () => {
        socket.write(payload);
      });
    });
  }
}

interface ExecError {
  code?: number;
  stdout?: string;
  stderr?: string;
}

function isExecError(error: unknown): error is ExecError {
  return typeof error === "object" && error !== null && ("stdout" in error || "stderr" in error);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function assertRuntimeStatus(value: unknown): RuntimeStatus {
  if (
    !isRecord(value) ||
    typeof value.running !== "boolean" ||
    typeof value.runtimePath !== "string" ||
    typeof value.socketPath !== "string" ||
    typeof value.protocolVersion !== "string" ||
    !(typeof value.pid === "number" || value.pid === null)
  ) {
    throw new TrunksRpcError(ErrorCode.InternalError, "invalid runtime.status response");
  }
  return value as unknown as RuntimeStatus;
}

function assertRepoStatus(value: unknown): RepoStatus {
  if (
    !isRecord(value) ||
    typeof value.repository !== "string" ||
    typeof value.path !== "string" ||
    typeof value.branch !== "string" ||
    typeof value.dirty !== "boolean" ||
    !(typeof value.head === "string" || value.head === null) ||
    !(typeof value.backend === "string" || value.backend === null)
  ) {
    throw new TrunksRpcError(ErrorCode.InternalError, "invalid repo.status response");
  }
  return value as unknown as RepoStatus;
}

function assertDiffEntry(value: unknown): DiffEntry {
  if (
    !isRecord(value) ||
    !(value.status === "A" || value.status === "M" || value.status === "D") ||
    typeof value.path !== "string"
  ) {
    throw new TrunksRpcError(ErrorCode.InternalError, "invalid vcs.diff entry");
  }
  return value as unknown as DiffEntry;
}
