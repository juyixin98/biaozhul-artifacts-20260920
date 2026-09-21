-- Reference counting for content-addressed blobs.
--
-- blobs.refcount must always equal the number of rows referencing the blob
-- from dataset_chunks.blob_sha256 and datasets.whole_sha256. Maintaining that
-- invariant in application code would require careful updates on every path
-- (chunk upsert, publish, delete, cascade); triggers keep it structurally
-- true even under concurrent transactions.
--
-- INSERT ... ON CONFLICT DO UPDATE on dataset_chunks (retransmit / adopt
-- existing blob) is handled by a generic trigger.

CREATE OR REPLACE FUNCTION blob_refcount_after_change()
RETURNS trigger AS $$
BEGIN
    IF (TG_OP = 'DELETE') THEN
        IF OLD.blob_sha256 IS NOT NULL THEN
            UPDATE blobs SET refcount = refcount - 1
             WHERE sha256 = OLD.blob_sha256;
        END IF;
        RETURN OLD;
    ELSIF (TG_OP = 'UPDATE') THEN
        IF NEW.blob_sha256 IS DISTINCT FROM OLD.blob_sha256 THEN
            IF OLD.blob_sha256 IS NOT NULL THEN
                UPDATE blobs SET refcount = refcount - 1
                 WHERE sha256 = OLD.blob_sha256;
            END IF;
            IF NEW.blob_sha256 IS NOT NULL THEN
                UPDATE blobs SET refcount = refcount + 1
                 WHERE sha256 = NEW.blob_sha256;
            END IF;
        END IF;
        RETURN NEW;
    ELSIF (TG_OP = 'INSERT') THEN
        IF NEW.blob_sha256 IS NOT NULL THEN
            UPDATE blobs SET refcount = refcount + 1
             WHERE sha256 = NEW.blob_sha256;
        END IF;
        RETURN NEW;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_dataset_chunks_refcount
AFTER INSERT OR UPDATE OR DELETE ON dataset_chunks
FOR EACH ROW EXECUTE FUNCTION blob_refcount_after_change();

CREATE OR REPLACE FUNCTION dataset_whole_refcount_after_change()
RETURNS trigger AS $$
BEGIN
    IF (TG_OP = 'DELETE') THEN
        IF OLD.whole_sha256 IS NOT NULL THEN
            UPDATE blobs SET refcount = refcount - 1
             WHERE sha256 = OLD.whole_sha256;
        END IF;
        RETURN OLD;
    ELSIF (TG_OP = 'UPDATE') THEN
        IF NEW.whole_sha256 IS DISTINCT FROM OLD.whole_sha256 THEN
            IF OLD.whole_sha256 IS NOT NULL THEN
                UPDATE blobs SET refcount = refcount - 1
                 WHERE sha256 = OLD.whole_sha256;
            END IF;
            IF NEW.whole_sha256 IS NOT NULL then
                UPDATE blobs SET refcount = refcount + 1
                 WHERE sha256 = NEW.whole_sha256;
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_datasets_whole_refcount
AFTER UPDATE OR DELETE ON datasets
FOR EACH ROW EXECUTE FUNCTION dataset_whole_refcount_after_change();

-- Referential integrity is already enforced by the inline FOREIGN KEY
-- constraints in 0001; triggers above keep the counters correct.
