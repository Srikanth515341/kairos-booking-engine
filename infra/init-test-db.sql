-- Runs once, on first container init, alongside the default kairos_dev database.
-- Phase 2 onward needs a separate kairos_test database so the test suite never
-- touches development data.
CREATE DATABASE kairos_test OWNER kairos;
