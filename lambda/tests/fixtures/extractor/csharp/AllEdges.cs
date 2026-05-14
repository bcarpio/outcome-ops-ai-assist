using System;
using System.Linq;
using Hyatt.Util;

namespace Hyatt.Booking
{
    public class BookingService : BaseService, IBookable
    {
        public void Confirm()
        {
            Logger.Log("confirm");
            this.NotifyOps();
        }

        private void NotifyOps()
        {
            Logger.Log("notified");
        }
    }

    public interface IBookable
    {
    }
}
