"""
Health monitoring for the bot
Tracks performance metrics and alerts on issues
"""
import time
from collections import deque
from datetime import datetime
from typing import Optional, Dict, Any

class HealthMonitor:
    def __init__(self):
        self.request_times = deque(maxlen=1000)  # Keep last 1000 requests
        self.error_count = 0
        self.total_requests = 0
        self.start_time = time.time()
        self.last_stats_print = time.time()
        self.stats_interval = 300  # Print stats every 5 minutes
        
    def record_request(self, duration: float, success: bool = True):
        """Record a request's duration"""
        self.request_times.append(duration)
        self.total_requests += 1
        if not success:
            self.error_count += 1
        
        # Auto-print stats every interval
        if time.time() - self.last_stats_print > self.stats_interval:
            self.print_stats()
            self.last_stats_print = time.time()
    
    def get_stats(self) -> Optional[Dict[str, Any]]:
        """Get current health statistics"""
        if not self.request_times:
            return None
            
        avg_time = sum(self.request_times) / len(self.request_times)
        max_time = max(self.request_times)
        min_time = min(self.request_times)
        uptime = time.time() - self.start_time
        error_rate = (self.error_count / self.total_requests * 100) if self.total_requests > 0 else 0
        
        # Calculate requests per minute
        rpm = (self.total_requests / uptime) * 60 if uptime > 0 else 0
        
        return {
            'avg_response_time': avg_time,
            'max_response_time': max_time,
            'min_response_time': min_time,
            'total_requests': self.total_requests,
            'error_count': self.error_count,
            'error_rate': error_rate,
            'uptime_hours': uptime / 3600,
            'requests_per_minute': rpm
        }
    
    def print_stats(self):
        """Print health statistics"""
        stats = self.get_stats()
        if stats:
            print("\n" + "=" * 60)
            print("📊 BOT HEALTH STATISTICS")
            print("=" * 60)
            print(f"⏱️  Avg Response Time: {stats['avg_response_time']:.2f}s")
            print(f"⏱️  Min Response Time: {stats['min_response_time']:.2f}s")
            print(f"⏱️  Max Response Time: {stats['max_response_time']:.2f}s")
            print(f"📈 Total Requests: {stats['total_requests']}")
            print(f"📊 Requests/Minute: {stats['requests_per_minute']:.1f}")
            print(f"❌ Errors: {stats['error_count']} ({stats['error_rate']:.1f}%)")
            print(f"⏰ Uptime: {stats['uptime_hours']:.1f} hours")
            
            # Health status
            if stats['error_rate'] > 10:
                print("⚠️  Status: UNHEALTHY (High error rate)")
            elif stats['avg_response_time'] > 3:
                print("⚠️  Status: DEGRADED (Slow response)")
            else:
                print("✅ Status: HEALTHY")
            
            print("=" * 60 + "\n")
    
    def reset_stats(self):
        """Reset all statistics"""
        self.request_times.clear()
        self.error_count = 0
        self.total_requests = 0
        self.start_time = time.time()
        print("🔄 Health monitor statistics reset")

# Global monitor instance
health_monitor = HealthMonitor()

def get_health_monitor() -> HealthMonitor:
    """Get the global health monitor instance"""
    return health_monitor
