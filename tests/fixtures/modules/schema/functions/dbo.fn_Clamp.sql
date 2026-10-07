create or alter function dbo.fn_Clamp (@v AS decimal(19, 4), @lo AS decimal(19, 4) = 0, @hi AS decimal(19, 4) = 100)
returns decimal(19, 4)
with returns null on null input, schemabinding
as
begin
    return case when @v < @lo then @lo when @v > @hi then @hi else @v end;
end;
