# create_migration.py
from app import create_app
from app.extensions import db

app = create_app()

with app.app_context():
    # This will add the new columns to existing table
    with db.engine.connect() as conn:
        # Check if columns exist
        result = conn.execute(db.text("PRAGMA table_info(run_results)"))
        existing_columns = [row[1] for row in result]

        if 'original_pdf_path' not in existing_columns:
            conn.execute(db.text("ALTER TABLE run_results ADD COLUMN original_pdf_path VARCHAR(500)"))
            conn.commit()
            print("✓ Added original_pdf_path column")

        if 'expected_pdf_path' not in existing_columns:
            conn.execute(db.text("ALTER TABLE run_results ADD COLUMN expected_pdf_path VARCHAR(500)"))
            conn.commit()
            print("✓ Added expected_pdf_path column")

        if 'visual_diff_images' not in existing_columns:
            conn.execute(db.text("ALTER TABLE run_results ADD COLUMN visual_diff_images TEXT"))
            conn.commit()
            print("✓ Added visual_diff_images column")

    print("\n✅ Database migration completed!")
