/**
 * Shared invoice domain types.
 *
 * These mirror the backend (FastAPI) response schemas exactly. Monetary fields
 * (`subtotal`, `tax_amount`, `total_amount`) are serialized by the backend as
 * `Decimal` -> string, so they stay `string | null` here. Do not parse them to
 * `number` in the API layer.
 */

export type InvoiceStatus =
  | "ingested"
  | "extracted"
  | "validated"
  | "needs_review"
  | "approved"
  | "rejected"
  | "failed";

export type DocumentProcessingStatus = "uploaded" | "text_extracted" | "failed";

export interface LineItem {
  id: string;
  position: number;
  description: string | null;
  quantity: string | null;
  unit_price: string | null;
  line_total: string | null;
  tax_rate: string | null;
}

export interface Invoice {
  id: string;
  status: InvoiceStatus;
  created_at: string;
  updated_at: string;
  document_id: string;
  document_filename: string;
  document_content_type: string;
  document_size_bytes: number;
  document_processing_status: DocumentProcessingStatus;
  vendor_name: string | null;
  invoice_number: string | null;
  po_number: string | null;
  invoice_date: string | null;
  due_date: string | null;
  currency: string | null;
  subtotal: string | null;
  tax_amount: string | null;
  total_amount: string | null;
  line_items?: LineItem[];
}

export interface InvoiceListResponse {
  items: Invoice[];
  total: number;
  limit: number;
  offset: number;
}
