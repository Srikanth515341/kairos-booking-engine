import type { DisplayBlock } from './DisplayBlock'

export type Selection =
  | { kind: 'new'; start: Date; end: Date }
  | { kind: 'manage'; bookingId: string; block: DisplayBlock }
  | null

export interface OpenSlotOption {
  start: string
  end: string
}

export interface ConflictState {
  message: string
  nearestSlots: OpenSlotOption[]
}
