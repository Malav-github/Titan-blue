"""SQLite database management for Project Titan"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any

class TitanDatabase:
    """SQLite database for storing research findings"""

    def __init__(self, db_path: str):
        """Initialize database connection"""
        self.db_path = db_path
        self.connection = None
        self.initialize()

    def initialize(self):
        """Create database connection and initialize schema"""
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.create_schema()

    def create_schema(self):
        """Create database tables if they don't exist"""
        cursor = self.connection.cursor()

        # Companies table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            website TEXT,
            industry TEXT,
            founded_year INTEGER,
            size TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # Findings table (stores research results)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            claim TEXT NOT NULL,
            confidence_score INTEGER,
            confidence_tier TEXT,
            source_url TEXT,
            source_title TEXT,
            source_date TEXT,
            source_type TEXT,
            fingerprint TEXT UNIQUE,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (company_id) REFERENCES companies(id)
        )
        """)

        # Sources table (citation metadata)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            finding_id INTEGER NOT NULL,
            url TEXT,
            title TEXT,
            author TEXT,
            publication_date TEXT,
            source_type TEXT,
            credibility_score INTEGER,
            accessed_date TIMESTAMP,
            still_accessible BOOLEAN,
            archive_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (finding_id) REFERENCES findings(id)
        )
        """)

        # Research metadata (tracking analysis runs)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS research_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            analysis_type TEXT,
            analysis_date TIMESTAMP,
            finding_count INTEGER,
            cost REAL,
            duration_minutes INTEGER,
            kb_reused BOOLEAN,
            status TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (company_id) REFERENCES companies(id)
        )
        """)

        # Create indexes for performance
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_findings_company
        ON findings(company_id)
        """)

        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_findings_fingerprint
        ON findings(fingerprint)
        """)

        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_findings_confidence
        ON findings(confidence_score)
        """)

        self.connection.commit()

    def add_company(self, name: str, website: str = None, industry: str = None) -> int:
        """Add a company to the database"""
        cursor = self.connection.cursor()
        cursor.execute("""
        INSERT OR IGNORE INTO companies (name, website, industry)
        VALUES (?, ?, ?)
        """, (name, website, industry))
        self.connection.commit()

        # Get the company ID
        cursor.execute("SELECT id FROM companies WHERE name = ?", (name,))
        return cursor.fetchone()[0]

    def add_finding(self, company_id: int, claim: str, confidence_score: int,
                   confidence_tier: str, source_url: str = None,
                   source_title: str = None, fingerprint: str = None) -> Optional[int]:
        """Add a research finding"""
        cursor = self.connection.cursor()

        try:
            cursor.execute("""
            INSERT INTO findings
            (company_id, claim, confidence_score, confidence_tier,
             source_url, source_title, fingerprint)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (company_id, claim, confidence_score, confidence_tier,
                  source_url, source_title, fingerprint))
            self.connection.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            # Finding with this fingerprint already exists
            return None

    def get_company_findings(self, company_id: int) -> List[Dict[str, Any]]:
        """Get all findings for a company"""
        cursor = self.connection.cursor()
        cursor.execute("""
        SELECT * FROM findings
        WHERE company_id = ? AND status = 'active'
        ORDER BY confidence_score DESC
        """, (company_id,))

        return [dict(row) for row in cursor.fetchall()]

    def get_company_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Get company by name"""
        cursor = self.connection.cursor()
        cursor.execute("SELECT * FROM companies WHERE name = ?", (name,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def close(self):
        """Close database connection"""
        if self.connection:
            self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

# Convenience functions
def init_database(db_path: str) -> TitanDatabase:
    """Initialize database"""
    return TitanDatabase(db_path)

if __name__ == "__main__":
    from config import DATABASE_PATH

    print("🗄️  Initializing Database")
    print("==========================\n")

    db = TitanDatabase(DATABASE_PATH)
    print(f"✓ Database initialized at: {DATABASE_PATH}")
    print("✓ Schema created successfully")
    print("\nTables created:")
    print("  - companies")
    print("  - findings")
    print("  - sources")
    print("  - research_metadata")

    db.close()
