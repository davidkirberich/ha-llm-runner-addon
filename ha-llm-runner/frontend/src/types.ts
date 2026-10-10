export interface TaskStatus {
  state?: string;
  error?: string;
  finished_at?: string;
  duration?: number;
  last_result?: unknown;
  last_prompt?: string;
  attachments?: string[];
}

export interface Task {
  id: string;
  llm: boolean;
  provider: string | null;
  model: string | null;
  memory_entries: number;
  status: TaskStatus;
}

export interface Overview {
  tasks: Task[];
  config_errors: { line: number | null; message: string }[];
  config_warnings: string[];
  mqtt: { connected: boolean; error?: string | null };
}

export interface TaskDetail {
  id: string;
  config: { prompt?: string; history_limit?: number };
  status: TaskStatus;
  memory: { time?: string; text?: string }[];
  settings_text: string;
}

export interface EntityRow {
  section: string;
  alias: string;
  target: string;
  kind: string;
  entity_id: string | null;
  missing?: boolean;
  name?: string | null;
  state?: unknown;
  unit?: string | null;
}

export interface EntityRows {
  rows: EntityRow[];
  error: string | null;
  list_format: boolean;
}

export interface YamlCheck {
  valid: boolean;
  line?: number;
  column?: number;
  message?: string;
  entities?: EntityRows | null;
}

export interface SearchEntity {
  entity_id: string;
  name: string;
  state: unknown;
  unit?: string | null;
}

export interface Preview {
  ok: boolean;
  duration?: number;
  prompt?: string;
  result?: unknown;
  attachments?: string[];
  error?: string;
  traceback?: string;
  warnings?: string[];
}
