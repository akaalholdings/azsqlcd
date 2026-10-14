create clustered index CIX_OrderLine on sales.OrderLine (OrderDate) with (sort_in_tempdb = on) on PS_OrderYear (OrderDate)
