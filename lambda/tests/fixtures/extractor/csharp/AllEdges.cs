using System;
using System.Linq;
using Acme.Util;

namespace Acme.Booking
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
