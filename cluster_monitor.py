#!/usr/bin/env python3
"""
Cluster Monitoring Script

This script:
1. Extracts cluster information using pw CLI commands
2. Filters for 'existing' type clusters with 'on' status
3. Executes SSH commands to get usage information
4. Parses the output into JSON format for dashboard integration
5. Processes clusters in round-robin fashion
"""

import subprocess
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

class ClusterMonitor:
    def __init__(self):
        self.clusters = []
        self.current_cluster_index = 0
        self.max_retries = 3
        self.retry_delay = 5  # seconds

    def get_active_clusters(self) -> List[Dict[str, str]]:
        """
        Get active clusters using pw CLI command
        Filters for type='existing' and status='on'
        """
        try:
            # Execute pw clusters ls command
            cmd = [
                'pw', 'clusters', 'ls',
                '--status=on',
                '-o', 'table',
                '--owned'
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, check=True)

            # Parse the table output
            clusters = self._parse_cluster_table(result.stdout)
            return clusters

        except subprocess.CalledProcessError as e:
            print(f"Error getting clusters: {e}")
            return []
        except Exception as e:
            print(f"Unexpected error: {e}")
            return []

    def _parse_cluster_table(self, table_output: str) -> List[Dict[str, str]]:
        """
        Parse the cluster table output from pw CLI
        """
        clusters = []
        lines = table_output.strip().split('\n')

        # Skip header lines and separator lines
        data_lines = []
        for line in lines:
            # Skip separator lines (+---+---+)
            if line.startswith('+') or line.startswith('|') and '+' in line:
                continue
            # Skip empty lines
            if not line.strip():
                continue
            # Skip header lines containing URI, STATUS, TYPE
            if 'URI' in line or 'STATUS' in line or 'TYPE' in line:
                continue
            # This should be a data line
            data_lines.append(line)

        # Parse each cluster line - format is like: | pw://mshaxted/jean | on     | existing  |
        for line in data_lines:
            # Clean up the line by removing leading/trailing whitespace and pipes
            clean_line = line.strip().strip('|').strip()
            if not clean_line:
                continue

            # Split by pipe character to get the columns
            parts = [part.strip() for part in clean_line.split('|') if part.strip()]

            # We expect at least 3 parts: URI, STATUS, TYPE
            if len(parts) >= 3:
                uri = parts[0].strip()
                status = parts[1].strip()
                cluster_type = parts[2].strip()

                # Filter for existing type and on status
                if cluster_type == 'existing' and status == 'on':
                    clusters.append({
                        'uri': uri,
                        'status': status,
                        'type': cluster_type
                    })

        return clusters

    def get_cluster_usage(self, cluster_uri: str) -> Optional[Dict[str, Any]]:
        """
        Get usage information for a specific cluster using SSH
        """
        if not cluster_uri:
            return None

        try:
            # Execute SSH command to get usage
            cmd = [
                'pw', 'ssh', cluster_uri, 'show_usage'
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, check=True)

            # Parse the usage output
            usage_data = self._parse_usage_output(result.stdout)
            return usage_data

        except subprocess.CalledProcessError as e:
            print(f"Error getting usage for {cluster_uri}: {e}")
            return None
        except Exception as e:
            print(f"Unexpected error for {cluster_uri}: {e}")
            return None

    def get_cluster_queues(self, cluster_uri: str) -> Optional[Dict[str, Any]]:
        """
        Get queue information for a specific cluster using SSH
        """
        if not cluster_uri:
            return None

        try:
            # Execute SSH command to get queue information
            cmd = [
                'pw', 'ssh', cluster_uri, 'show_queues'
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, check=True)

            # Parse the queue output
            queue_data = self._parse_queue_output(result.stdout)
            return queue_data

        except subprocess.CalledProcessError as e:
            print(f"Error getting queues for {cluster_uri}: {e}")
            return None
        except Exception as e:
            print(f"Unexpected error for {cluster_uri}: {e}")
            return None

    def _parse_queue_output(self, queue_output: str) -> Dict[str, Any]:
        """
        Parse the queue output from SSH command into structured JSON
        """
        queue_data = {
            'queue_info': [],
            'node_info': []
        }

        lines = queue_output.strip().split('\n')

        # Parse QUEUE INFORMATION section
        queue_section = False
        node_section = False

        for i, line in enumerate(lines):
            # Check for section headers
            if 'QUEUE INFORMATION:' in line:
                queue_section = True
                node_section = False
                continue
            elif 'NODE INFORMATION:' in line:
                queue_section = False
                node_section = True
                continue

            # Process QUEUE INFORMATION section
            if queue_section:
                # Skip header and separator lines
                if line.startswith('Queue Name') or line.startswith('=') or line.startswith('|'):
                    continue

                # Skip empty lines
                if not line.strip():
                    continue

                # Parse queue data line
                clean_line = line.strip()
                if clean_line:
                    # Format: HIE               24:00:00     -     0   2304     4     0    384       0 Exe Y Y
                    parts = clean_line.split()
                    if len(parts) >= 12:
                        try:
                            queue_info = {
                                'queue_name': parts[0].strip(),
                                'max_walltime': parts[1].strip(),
                                'max_jobs': parts[2].strip(),
                                'min_cores': parts[3].strip(),
                                'max_cores': parts[4].strip(),
                                'jobs_running': int(parts[5].strip()),
                                'jobs_pending': int(parts[6].strip()),
                                'cores_running': int(parts[7].strip()),
                                'cores_pending': int(parts[8].strip()),
                                'queue_type': parts[9].strip(),
                                'enabled': parts[10].strip() == 'Y',
                                'reserved': parts[11].strip() == 'Y'
                            }
                            queue_data['queue_info'].append(queue_info)
                        except (ValueError, IndexError) as e:
                            continue

            # Process NODE INFORMATION section
            elif node_section:
                # Skip header and separator lines
                if line.startswith('Node Type') or line.startswith('=') or line.startswith('|'):
                    continue

                # Skip empty lines
                if not line.strip():
                    continue

                # Parse node data line
                clean_line = line.strip()
                if clean_line:
                    # Format: Standard                 494           96        47424        10080        37344
                    parts = clean_line.split()
                    if len(parts) >= 6:
                        try:
                            node_info = {
                                'node_type': parts[0].strip(),
                                'nodes_available': int(parts[1].strip()),
                                'cores_per_node': int(parts[2].strip()),
                                'cores_available': int(parts[3].strip()),
                                'cores_running': int(parts[4].strip()),
                                'cores_free': int(parts[5].strip())
                            }
                            queue_data['node_info'].append(node_info)
                        except (ValueError, IndexError) as e:
                            continue

        return queue_data

    def _parse_usage_output(self, usage_output: str) -> Dict[str, Any]:
        """
        Parse the usage output from SSH command into structured JSON
        """
        usage_data = {
            'header': '',
            'fiscal_year_info': '',
            'systems': []
        }

        lines = usage_output.strip().split('\n')

        # Extract header information (first non-empty lines)
        header_lines = []
        for line in lines:
            if line.strip() and not line.startswith('System') and not line.startswith('='):
                header_lines.append(line.strip())
            else:
                break

        usage_data['header'] = ' '.join(header_lines)

        # Extract fiscal year info
        fiscal_lines = []
        for line in lines:
            if 'Fiscal Year' in line or 'Hours Remaining' in line:
                fiscal_lines.append(line.strip())

        usage_data['fiscal_year_info'] = ' '.join(fiscal_lines)

        # Parse system usage table - look for the table header
        system_data = []
        in_table = False
        table_started = False
        separator_found = False

        for line in lines:
            # Look for table header line
            if 'System' in line and 'Subproject' in line and 'Allocated' in line:
                in_table = True
                table_started = True
                separator_found = False  # Reset separator flag when we find header
                continue

            # If we're in table mode, process data lines
            if in_table and table_started:
                # Check for separator lines (======== or --------)
                if line.startswith('=') or line.startswith('--------'):
                    separator_found = True
                    continue  # Don't end table mode, just skip separator

                # Skip empty lines
                if not line.strip():
                    continue

                # If we found separator and have data, process it
                if separator_found:
                    # Parse system usage line - format from SSH output:
                    # jean          AFSNW27526RYZ     250000          0     250000  100.00%          0
                    clean_line = line.strip()
                    if clean_line:
                        parts = clean_line.split()
                        if len(parts) >= 7:
                            try:
                                system_info = {
                                    'system': parts[0].strip(),
                                    'subproject': parts[1].strip(),
                                    'hours_allocated': int(parts[2].strip()),
                                    'hours_used': int(parts[3].strip()),
                                    'hours_remaining': int(parts[4].strip()),
                                    'percent_remaining': float(parts[5].strip().rstrip('%')),
                                    'background_hours_used': int(parts[6].strip())
                                }
                                system_data.append(system_info)
                            except (ValueError, IndexError) as e:
                                # Skip lines that don't parse correctly
                                continue

        usage_data['systems'] = system_data
        return usage_data

    def _parse_queue_output(self, queue_output: str) -> Dict[str, Any]:
        """
        Parse the queue output from SSH command into structured JSON
        """
        queue_data = {
            'queues': [],
            'nodes': []
        }

        lines = queue_output.strip().split('\n')

        # Parse queue information section
        in_queue_section = False
        in_node_section = False

        for line in lines:
            # Check for queue section header
            if 'QUEUE INFORMATION:' in line or 'Queue Name' in line:
                in_queue_section = True
                in_node_section = False
                continue

            # Check for node section header
            if 'NODE INFORMATION:' in line or 'Node Type' in line:
                in_node_section = True
                in_queue_section = False
                continue

            # Process queue section
            if in_queue_section:
                # Skip separator lines and empty lines
                if line.startswith('=') or line.startswith('-') or line.startswith('|') or not line.strip():
                    continue

                # Parse queue data line
                if 'Queue Name' not in line and line.strip():
                    parts = line.split()
                    if len(parts) >= 10:  # We expect at least 10 columns
                        try:
                            queue_info = {
                                'queue_name': parts[0].strip(),
                                'max_walltime': parts[1].strip(),
                                'max_jobs': parts[2].strip(),
                                'max_cores': parts[3].strip(),
                                'max_cores_per_job': parts[4].strip(),
                                'jobs_running': parts[5].strip(),
                                'jobs_pending': parts[6].strip(),
                                'cores_running': parts[7].strip(),
                                'cores_pending': parts[8].strip(),
                                'queue_type': parts[9].strip()
                            }
                            queue_data['queues'].append(queue_info)
                        except (ValueError, IndexError) as e:
                            continue

            # Process node section
            if in_node_section:
                # Skip separator lines and empty lines
                if line.startswith('=') or line.startswith('-') or line.startswith('|') or not line.strip():
                    continue

                # Parse node data line
                if 'Node Type' not in line and line.strip():
                    parts = line.split()
                    if len(parts) >= 5:  # We expect at least 5 columns
                        try:
                            node_info = {
                                'node_type': parts[0].strip(),
                                'nodes_available': parts[1].strip(),
                                'cores_per_node': parts[2].strip(),
                                'cores_available': parts[3].strip(),
                                'cores_running': parts[4].strip(),
                                'cores_free': parts[5].strip() if len(parts) > 5 else '0'
                            }
                            queue_data['nodes'].append(node_info)
                        except (ValueError, IndexError) as e:
                            continue

        return queue_data

    def process_clusters_round_robin(self) -> List[Dict[str, Any]]:
        """
        Process all clusters in round-robin fashion
        Returns list of cluster usage/queue documents.
        """
        results: List[Dict[str, Any]] = []

        # Get active clusters
        clusters = self.get_active_clusters()
        if not clusters:
            print("No active clusters found")
            return results

        print(f"Found {len(clusters)} active clusters to process")

        # Process each cluster
        for i, cluster in enumerate(clusters):
            cluster_name = cluster['uri'].split('/')[-1]  # Extract cluster name from URI
            print(f"Processing cluster {i+1}/{len(clusters)}: {cluster_name}")

            # Get usage data
            usage_data = self.get_cluster_usage(cluster['uri'])
            if usage_data:
                print(f"Successfully got usage data for {cluster_name}")
            else:
                print(f"Failed to get usage data for {cluster_name}")

            # Get queue data
            queue_data = self.get_cluster_queues(cluster['uri'])
            if queue_data:
                print(f"Successfully got queue data for {cluster_name}")
            else:
                print(f"Failed to get queue data for {cluster_name}")

            # Add to results
            results.append({
                'cluster_metadata': {
                    'name': cluster_name,
                    'uri': cluster['uri'],
                    'status': cluster['status'],
                    'type': cluster['type'],
                    'timestamp': datetime.utcnow().isoformat()
                },
                'usage_data': usage_data or {},
                'queue_data': queue_data or {}
            })

            # Small delay between clusters
            time.sleep(1)

        return results

    def save_results_to_json(self, results: List[Dict[str, Any]], filename: Optional[str] = None) -> bool:
        """
        Save results to JSON file with enhanced structure
        """
        output_path = Path(filename) if filename else Path(__file__).resolve().parent / "public" / "data" / "cluster_usage.json"
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open('w', encoding='utf-8') as f:
                json.dump(results, f, indent=2)
            print(f"Enhanced results saved to {output_path}")
            return True
        except Exception as e:
            print(f"Error saving enhanced results: {e}")
            return False

    def run(self) -> bool:
        """
        Main execution method
        """
        print("Starting cluster monitoring...")

        # Process clusters in round-robin
        results = self.process_clusters_round_robin()

        if results:
            # Save to JSON
            self.save_results_to_json(results)
            return True
        else:
            print("No results to save")
            return False

if __name__ == "__main__":
    # Create and run the monitor
    monitor = ClusterMonitor()

    # Run the monitoring process
    success = monitor.run()

    if success:
        print("Cluster monitoring completed successfully")
        sys.exit(0)
    else:
        print("Cluster monitoring failed")
        sys.exit(1)
