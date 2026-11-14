#!/usr/bin/env bash
# Five-minute demo. Fill in as phases land.
set -euo pipefail
echo "1. Register a source:        scrapeline source add config/sources/<new>.yaml"
echo "2. Discover + generate:      scrapeline agent generate <source-id>"
echo "3. Verify against golden:    scrapeline verify <source-id>"
echo "4. Break it + repair:        scrapeline drift apply <case>; scrapeline agent repair <source-id>"
echo "5. Run pipeline + dashboard: scrapeline run <source-id>; streamlit run src/scrapeline/serve/dashboard.py"
